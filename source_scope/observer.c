/* libsil_source_scope.so.1: the observer of SiL source scopes (#372).
 *
 * docs/proposals/native-source-scopes.md is the contract; include/sil/
 * source_scope.h is the source API. A library built with SIL_SOURCE_SCOPES=1
 * links this observer and calls it at the entry and the return of each scope.
 *
 * States:
 *   dormant   No session, or this process is not the session's runner. A
 *             scope costs one relaxed atomic load and one branch.
 *   active    `sil profile --collector scopes` started this runner. A scope
 *             costs one direct clock_gettime system call, one try-lock and
 *             one 16-byte store into the shared mapping of events.bin.
 *   stopped   The buffer is full, or the observer is finalized. A scope
 *             costs one relaxed atomic load and a loss counter increment.
 *
 * The session (SIL_SOURCE_SCOPE_SESSION) is "capture=HEX32,capacity=N,fd=K,
 * pid=P": the command adds the first three, sil-diagnostics-launcher adds
 * the runner's PID. At the first entry, the active observer checks the file
 * and its header, sets FD_CLOEXEC and maps the whole file once (MAP_SHARED |
 * MAP_POPULATE). That first entry is the only one that calls the kernel for
 * more than the clock. If a check or the mapping fails, the observer stays
 * dormant and, when it can, stores the header state `unavailable`.
 *
 * Apart from that activation, a scope never allocates, calls a file system
 * function, retries or waits. A contended try-lock or a full buffer counts a
 * loss; there is no overwrite. Finalization, when this library is unloaded or
 * at process exit, is one store of the state `finalized` into the mapping.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <link.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define SIL_SOURCE_SCOPES 1
#include "sil/source_scope.h"

#define EXPORT __attribute__((visibility("default")))

enum { kHeaderBytes = 256, kRecordBytes = 16, kMaxCapacity = 262144 };
enum { kCreated = 0, kActive = 1, kFinalized = 2, kUnavailable = 3 };
enum { kEntry = 0, kReturn = 1 };

/* The raw header of events.bin, version 1, little-endian. */
struct header {
  char magic[8];
  uint16_t version, header_bytes, record_bytes, state;
  uint8_t capture[16];
  uint32_t pid, flags;
  uint64_t capacity, records, dropped_full, dropped_contended, clock_errors;
  uint64_t bracket[3];
  uint64_t pidns_device, pidns_inode;
  uint8_t build_id_bytes;
  uint8_t build_id[40];
  uint8_t reserved[95];
} __attribute__((packed));
_Static_assert(sizeof(struct header) == kHeaderBytes, "raw header layout");

struct record {
  uint64_t monotonic_ns;
  uint32_t tid;
  uint16_t scope_id;
  uint8_t edge, reserved;
};
_Static_assert(sizeof(struct record) == kRecordBytes, "raw record layout");

/* The observer's state machine. A scope reads it with one relaxed load. */
enum mode { kDormant, kPending, kActivating, kRecording, kStopped };
static _Atomic int mode = kDormant;

static struct session {
  uint8_t capture[16];
  uint64_t capacity;
  int fd;
} session;

static struct header *mapped;     /* the whole file, once active */
static struct record *records;    /* `capacity` records after the header */
static atomic_flag busy = ATOMIC_FLAG_INIT;
static _Atomic uint64_t early_drops;  /* entries while another activates */
static __thread uint32_t thread_id;

static int hex_digit(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

/* The decimal value of `key=` in `text`, ended by ',' or the end. */
static int field(const char *text, const char *key, unsigned long long *out) {
  const size_t length = strlen(key);
  for (const char *at = text; at != NULL; at = strchr(at, ',')) {
    if (*at == ',') at++;
    if (strncmp(at, key, length) != 0 || at[length] != '=') continue;
    char *end;
    errno = 0;
    *out = strtoull(at + length + 1, &end, 10);
    return errno == 0 && end != at + length + 1 && (*end == ',' || !*end);
  }
  return 0;
}

static int capture_field(const char *text, uint8_t capture[16]) {
  const char *at = strstr(text, "capture=");
  if (at == NULL || (at != text && at[-1] != ',')) return 0;
  at += strlen("capture=");
  for (int i = 0; i < 16; i++) {
    const int high = hex_digit(at[2 * i]), low = hex_digit(at[2 * i + 1]);
    if (high < 0 || low < 0) return 0;
    capture[i] = (uint8_t)(high << 4 | low);
  }
  return at[32] == ',' || at[32] == '\0';
}

/* Decide once, at load: only the runner named by the session can become
 * active. Anything else stays dormant for the life of the process. */
__attribute__((constructor)) static void load(void) {
  const char *text = getenv("SIL_SOURCE_SCOPE_SESSION");
  unsigned long long pid, capacity, fd;
  if (text == NULL || !field(text, "pid", &pid) ||
      pid != (unsigned long long)getpid())
    return;
  if (!field(text, "capacity", &capacity) || !field(text, "fd", &fd) ||
      !capture_field(text, session.capture) || fd > INT32_MAX ||
      capacity < 1 || capacity > kMaxCapacity)
    return;
  session.capacity = capacity;
  session.fd = (int)fd;
  atomic_store_explicit(&mode, kPending, memory_order_relaxed);
}

static uint64_t direct_clock(int *failed) {
  struct timespec now;
  if (syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &now) != 0) {
    *failed = 1;
    return 0;
  }
  return (uint64_t)now.tv_sec * 1000000000u + (uint64_t)now.tv_nsec;
}

/* The GNU build ID of the loaded object that contains `address`. */
struct build_id_search {
  uintptr_t address;
  uint8_t bytes;
  uint8_t id[40];
};

static int find_build_id(struct dl_phdr_info *info, size_t size, void *data) {
  (void)size;
  struct build_id_search *search = data;
  int contains = 0;
  for (int i = 0; i < info->dlpi_phnum; i++) {
    const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
    const uintptr_t begin = info->dlpi_addr + ph->p_vaddr;
    if (ph->p_type == PT_LOAD && search->address >= begin &&
        search->address < begin + ph->p_memsz)
      contains = 1;
  }
  if (!contains) return 0;
  for (int i = 0; i < info->dlpi_phnum; i++) {
    const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
    if (ph->p_type != PT_NOTE) continue;
    const uint8_t *note = (const uint8_t *)(info->dlpi_addr + ph->p_vaddr);
    const uint8_t *end = note + ph->p_memsz;
    while (note + 12 <= end) {
      uint32_t name_size, desc_size, type;
      memcpy(&name_size, note, 4);
      memcpy(&desc_size, note + 4, 4);
      memcpy(&type, note + 8, 4);
      const uint8_t *name = note + 12;
      const uint8_t *desc = name + ((name_size + 3u) & ~3u);
      if (type == NT_GNU_BUILD_ID && name_size == 4 &&
          memcmp(name, "GNU", 4) == 0 && desc_size <= 40 &&
          desc + desc_size <= end) {
        search->bytes = (uint8_t)desc_size;
        memcpy(search->id, desc, desc_size);
        return 1;
      }
      note = desc + ((desc_size + 3u) & ~3u);
    }
  }
  return 1;
}

static void mark_unavailable(void) {
  const uint16_t state = kUnavailable;
  (void)pwrite(session.fd, &state, sizeof state,
               offsetof(struct header, state));
}

static int header_matches(const struct header *h) {
  return memcmp(h->magic, "SILSCOPE", 8) == 0 && h->version == 1 &&
         h->header_bytes == kHeaderBytes && h->record_bytes == kRecordBytes &&
         h->state == kCreated && h->flags == 0 &&
         memcmp(h->capture, session.capture, 16) == 0 &&
         h->capacity == session.capacity && h->records == 0;
}

/* The first entry in the runner: check, map and start the capture. */
static void activate(uintptr_t caller) {
  const size_t size = kHeaderBytes + kRecordBytes * session.capacity;
  struct stat file;
  if (fstat(session.fd, &file) != 0 || !S_ISREG(file.st_mode) ||
      (uint64_t)file.st_size != size) {
    mark_unavailable();
    atomic_store_explicit(&mode, kDormant, memory_order_release);
    return;
  }
  (void)fcntl(session.fd, F_SETFD, FD_CLOEXEC);
  void *memory = mmap(NULL, size, PROT_READ | PROT_WRITE,
                      MAP_SHARED | MAP_POPULATE, session.fd, 0);
  if (memory == MAP_FAILED) {
    mark_unavailable();
    atomic_store_explicit(&mode, kDormant, memory_order_release);
    return;
  }
  struct header *h = memory;
  if (!header_matches(h)) {
    __atomic_store_n(&h->state, (uint16_t)kUnavailable, __ATOMIC_RELEASE);
    munmap(memory, size);
    atomic_store_explicit(&mode, kDormant, memory_order_release);
    return;
  }
  struct build_id_search search = {caller, 0, {0}};
  dl_iterate_phdr(find_build_id, &search);
  struct stat pidns;
  if (stat("/proc/self/ns/pid", &pidns) == 0) {
    h->pidns_device = pidns.st_dev;
    h->pidns_inode = pidns.st_ino;
  }
  int failed = 0;
  struct timespec libc_now;
  h->bracket[0] = direct_clock(&failed);
  clock_gettime(CLOCK_MONOTONIC, &libc_now);
  h->bracket[1] =
      (uint64_t)libc_now.tv_sec * 1000000000u + (uint64_t)libc_now.tv_nsec;
  h->bracket[2] = direct_clock(&failed);
  h->build_id_bytes = search.bytes;
  memcpy(h->build_id, search.id, sizeof h->build_id);
  h->pid = (uint32_t)getpid();
  h->dropped_contended = atomic_exchange(&early_drops, 0);
  mapped = h;
  records = (struct record *)((char *)memory + kHeaderBytes);
  __atomic_store_n(&h->state, (uint16_t)kActive, __ATOMIC_RELEASE);
  atomic_store_explicit(&mode, kRecording, memory_order_release);
}

static void count(uint64_t *counter) {
  __atomic_fetch_add(counter, 1, __ATOMIC_RELAXED);
}

static void observe(uint16_t id, uint8_t edge, uintptr_t caller) {
  int current = atomic_load_explicit(&mode, memory_order_relaxed);
  if (current == kDormant) return;
  if (current == kPending) {
    int expected = kPending;
    if (atomic_compare_exchange_strong(&mode, &expected, kActivating))
      activate(caller);
    current = atomic_load_explicit(&mode, memory_order_acquire);
  }
  if (current == kActivating) {
    atomic_fetch_add_explicit(&early_drops, 1, memory_order_relaxed);
    return;
  }
  if (current == kStopped) {
    count(&mapped->dropped_full);
    return;
  }
  if (current != kRecording) return;
  int failed = 0;
  const uint64_t now = direct_clock(&failed);
  if (failed) {
    count(&mapped->clock_errors);
    return;
  }
  if (atomic_flag_test_and_set_explicit(&busy, memory_order_acquire)) {
    count(&mapped->dropped_contended);
    return;
  }
  if (thread_id == 0) thread_id = (uint32_t)gettid();
  const uint64_t used = __atomic_load_n(&mapped->records, __ATOMIC_RELAXED);
  if (used < session.capacity) {
    records[used] = (struct record){now, thread_id, id, edge, 0};
    __atomic_store_n(&mapped->records, used + 1, __ATOMIC_RELEASE);
  } else {
    count(&mapped->dropped_full);
    atomic_store_explicit(&mode, kStopped, memory_order_relaxed);
  }
  atomic_flag_clear_explicit(&busy, memory_order_release);
}

EXPORT sil_source_scope_v1_guard sil_source_scope_v1_enter(uint16_t id) {
  observe(id, kEntry, (uintptr_t)__builtin_return_address(0));
  return (sil_source_scope_v1_guard){id};
}

EXPORT void sil_source_scope_v1_return(const sil_source_scope_v1_guard *guard) {
  observe(guard->id, kReturn, (uintptr_t)__builtin_return_address(0));
}

/* At unload or exit: one store of the state into the mapping. No file I/O,
 * no wait. Later scopes are counted as losses. */
__attribute__((destructor)) static void finalize(void) {
  const int previous =
      atomic_exchange_explicit(&mode, kStopped, memory_order_acq_rel);
  if (previous == kRecording || previous == kStopped)
    __atomic_store_n(&mapped->state, (uint16_t)kFinalized, __ATOMIC_RELEASE);
  else
    atomic_store_explicit(&mode, kDormant, memory_order_relaxed);
}
