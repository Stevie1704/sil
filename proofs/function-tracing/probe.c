#define _GNU_SOURCE
#include "probe.h"
#include <dlfcn.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

/* No allocation, file I/O, retry or waiting in an event callback. One try-lock
 * protects a finite buffer; contention drops the event. No ring overwrite. */
#define CAPACITY 65536
typedef struct {
  uint64_t ns;
  uint32_t tid;
  uint16_t function;
  uint16_t edge; /* 0 entry, 1 exit, 2 tail exit */
} event;
_Static_assert(sizeof(event) == 16, "proof format requires 16-byte records");
static event events[CAPACITY];
static size_t used, limit = CAPACITY;
static atomic_flag busy = ATOMIC_FLAG_INIT;
static _Atomic unsigned long dropped;
static unsigned long clock_errors;
static uint64_t before, libc_time, after;
static int xray_max = -1, patch_status = -1;
static const char *output;

static uint64_t host_now(void) {
  struct timespec t;
  if (syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &t) != 0) {
    clock_errors++;
    return 0;
  }
  return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}

static void emit(uint16_t function, uint16_t edge) {
  if (!output) return;
  if (atomic_flag_test_and_set_explicit(&busy, memory_order_acquire)) {
    atomic_fetch_add(&dropped, 1);
    return;
  }
  if (used < limit) {
    events[used++] = (event){host_now(), (uint32_t)syscall(SYS_gettid),
                             function, edge};
  } else {
    atomic_fetch_add(&dropped, 1);
  }
  atomic_flag_clear_explicit(&busy, memory_order_release);
}

trace_scope trace_enter(uint16_t function) {
  emit(function, 0);
  return (trace_scope){function};
}
void trace_leave(trace_scope *scope) { emit(scope->function, 1); }

/* The custom XRay handler uses exactly the same monotonic clock and storage
 * as explicit probes. No TSC conversion or default XRay logger is involved. */
static void xray_event(int32_t function, int edge) {
  if (function <= 0 || function > UINT16_MAX || edge < 0 || edge > 2) {
    atomic_fetch_add(&dropped, 1);
    return;
  }
  emit((uint16_t)function, (uint16_t)edge);
}

void trace_xray_attach(void) {
  if (getenv("SIL_FUNCTION_XRAY")) {
    void (*init)(void) = dlsym(RTLD_DEFAULT, "__xray_init");
    if (init) init();
    size_t (*maximum)(void) = dlsym(RTLD_DEFAULT, "__xray_max_function_id");
    int (*handler)(void (*)(int32_t, int)) =
        dlsym(RTLD_DEFAULT, "__xray_set_handler");
    int (*patch)(void) = dlsym(RTLD_DEFAULT, "__xray_patch");
    if (maximum && handler && patch) {
      xray_max = (int)maximum();
      if (handler(xray_event)) patch_status = patch();
    }
  }
}

__attribute__((constructor)) static void start(void) {
  output = getenv("SIL_FUNCTION_TRACE");
  const char *n = getenv("SIL_FUNCTION_TRACE_LIMIT");
  if (n) {
    unsigned long value = strtoul(n, NULL, 10);
    if (value < limit) limit = value;
  }
  struct timespec t;
  before = host_now();
  clock_gettime(CLOCK_MONOTONIC, &t);
  libc_time = (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
  after = host_now();
  trace_xray_attach();
}

/* Two bounded writes, after execution has finished, to a fresh regular file.
 * A killed process / short write has no complete footer. The supervisor has
 * a 60 s timeout for the entire Run, including this final flush. */
__attribute__((destructor)) static void finish(void) {
  if (!output) return;
  int fd = open(output, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC,
                0600);
  if (fd < 0) return;
  ssize_t bytes = write(fd, events, used * sizeof(event));
  close(fd);
  char path[4096];
  if (snprintf(path, sizeof path, "%s.json", output) >= (int)sizeof path) return;
  fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
  if (fd < 0) return;
  char text[1024];
  int n = snprintf(text, sizeof text,
      "{\"clock\":\"host_monotonic\",\"pid\":%d,\"records\":%zu,"
      "\"capacity\":%zu,\"dropped\":%lu,\"bytes_written\":%zd,"
      "\"clock_errors\":%lu,\"clock_bracket_ns\":[%llu,%llu,%llu],"
      "\"xray_max_function_id\":%d,\"xray_patch_status\":%d}\n",
      getpid(), used, limit, atomic_load(&dropped), bytes, clock_errors,
      (unsigned long long)before, (unsigned long long)libc_time,
      (unsigned long long)after, xray_max, patch_status);
  if (n > 0 && n < (int)sizeof text) (void)write(fd, text, (size_t)n);
  close(fd);
}
