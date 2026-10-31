/* Observes the file reads of one SiL runner process, for
 * tools/hash_baseline.py. It is loaded with DYLD_INSERT_LIBRARIES or
 * LD_PRELOAD, so it measures any runner revision and no production code
 * changes. It is never part of a SiL build.
 *
 * For each path opened read-only it keeps the number of opens, the bytes read
 * and up to MAX_SPANS spans. A span is one contiguous read series of one open:
 * its start offset, its bytes, the file size at open and its host times. Tables
 * have a fixed size, and a full table counts what it drops. At exit the process
 * writes one JSON document to the path in SIL_HASH_TRACE_OUT. It observes only
 * the process that loaded it: it removes its variables from the environment, so
 * a Process participant does not load it, and a forked child records nothing.
 */
#ifndef __APPLE__
#define _GNU_SOURCE
#endif
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MAX_FILES 128
#define MAX_SPANS 256
#define MAX_FDS 1024
#define NO_SPAN (-1)

typedef struct {
  uint64_t offset, bytes, size, start_ns, end_ns;
} Span;

typedef struct {
  char path[PATH_MAX];
  uint64_t opens, reads, bytes, dropped_spans;
  int span_count;
  Span spans[MAX_SPANS];
} FileRecord;

typedef struct {
  FileRecord *file; /* NULL: this descriptor is not observed. */
  uint64_t size, position;
  int span;
} Descriptor;

static FileRecord files[MAX_FILES];
static Descriptor descriptors[MAX_FDS];
static int file_count;
static uint64_t dropped_files, process_start_ns;
static pid_t owner;
static int enabled;
static char out_path[PATH_MAX];
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
/* fread reaches read inside the C library on some platforms. */
static __thread int inside_stream_call;

#ifdef __APPLE__
#define REAL(name) name
#define OBSERVER(name) observed_##name
#define CLOCK_ID CLOCK_UPTIME_RAW
#else
#define REAL(name) real_##name
#define OBSERVER(name) name
#define CLOCK_ID CLOCK_MONOTONIC
static int (*real_open)(const char *, int, ...);
static FILE *(*real_fopen)(const char *, const char *);
static ssize_t (*real_read)(int, void *, size_t);
static ssize_t (*real_pread)(int, void *, size_t, off_t);
static size_t (*real_fread)(void *, size_t, size_t, FILE *);
static int (*real_close)(int);
static int (*real_fclose)(FILE *);
#endif

static uint64_t now_ns(void) {
  struct timespec t;
  clock_gettime(CLOCK_ID, &t);
  return (uint64_t)t.tv_sec * 1000000000u + (uint64_t)t.tv_nsec;
}

static int observing(void) { return enabled && getpid() == owner; }

static FileRecord *record_for(const char *path) {
  for (int i = 0; i < file_count; i++)
    if (strcmp(files[i].path, path) == 0) return &files[i];
  if (file_count == MAX_FILES) {
    dropped_files++;
    return NULL;
  }
  FileRecord *file = &files[file_count++];
  snprintf(file->path, sizeof file->path, "%s", path);
  return file;
}

static void note_open(int fd, const char *path) {
  if (fd < 0 || fd >= MAX_FDS || !observing()) return;
  struct stat st;
  char resolved[PATH_MAX];
  if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || !realpath(path, resolved))
    return;
  pthread_mutex_lock(&lock);
  FileRecord *file = record_for(resolved);
  if (file) file->opens++;
  descriptors[fd] = (Descriptor){file, (uint64_t)st.st_size, 0, NO_SPAN};
  pthread_mutex_unlock(&lock);
}

static void note_close(int fd) {
  if (fd < 0 || fd >= MAX_FDS || !observing()) return;
  pthread_mutex_lock(&lock);
  descriptors[fd].file = NULL;
  pthread_mutex_unlock(&lock);
}

static void note_read(int fd, uint64_t offset, ssize_t count, uint64_t start_ns,
                      uint64_t end_ns) {
  pthread_mutex_lock(&lock);
  Descriptor *d = &descriptors[fd];
  FileRecord *file = d->file;
  if (file && count >= 0) {
    Span *span = d->span == NO_SPAN ? NULL : &file->spans[d->span];
    if (!span || span->offset + span->bytes != offset) {
      if (file->span_count == MAX_SPANS) {
        span = NULL;
        d->span = NO_SPAN;
        if (count > 0) file->dropped_spans++;
      } else {
        d->span = file->span_count++;
        span = &file->spans[d->span];
        *span = (Span){offset, 0, d->size, start_ns, end_ns};
      }
    }
    if (span) {
      span->bytes += (uint64_t)count;
      span->end_ns = end_ns;
    }
    file->reads++;
    file->bytes += (uint64_t)count;
    d->position = offset + (uint64_t)count;
  }
  pthread_mutex_unlock(&lock);
}

static int watched(int fd) {
  return fd >= 0 && fd < MAX_FDS && !inside_stream_call && observing() &&
         descriptors[fd].file;
}

int OBSERVER(open)(const char *path, int flags, ...) {
  va_list arguments;
  va_start(arguments, flags);
  const int mode = (flags & O_CREAT) ? va_arg(arguments, int) : 0;
  va_end(arguments);
  const int fd = REAL(open)(path, flags, mode);
  if ((flags & O_ACCMODE) == O_RDONLY) note_open(fd, path);
  return fd;
}

FILE *OBSERVER(fopen)(const char *path, const char *mode) {
  inside_stream_call++;
  FILE *stream = REAL(fopen)(path, mode);
  inside_stream_call--;
  if (stream && mode[0] == 'r' && !strchr(mode, '+'))
    note_open(fileno(stream), path);
  return stream;
}

ssize_t OBSERVER(read)(int fd, void *buffer, size_t count) {
  if (!watched(fd)) return REAL(read)(fd, buffer, count);
  const uint64_t start = now_ns();
  const ssize_t n = REAL(read)(fd, buffer, count);
  note_read(fd, descriptors[fd].position, n, start, now_ns());
  return n;
}

ssize_t OBSERVER(pread)(int fd, void *buffer, size_t count, off_t offset) {
  if (!watched(fd)) return REAL(pread)(fd, buffer, count, offset);
  const uint64_t start = now_ns();
  const ssize_t n = REAL(pread)(fd, buffer, count, offset);
  note_read(fd, (uint64_t)offset, n, start, now_ns());
  return n;
}

size_t OBSERVER(fread)(void *buffer, size_t size, size_t count, FILE *stream) {
  const int fd = fileno(stream);
  if (!watched(fd)) return REAL(fread)(buffer, size, count, stream);
  const uint64_t start = now_ns();
  inside_stream_call++;
  const size_t n = REAL(fread)(buffer, size, count, stream);
  inside_stream_call--;
  note_read(fd, descriptors[fd].position, (ssize_t)(n * size), start, now_ns());
  return n;
}

int OBSERVER(close)(int fd) {
  note_close(fd);
  return REAL(close)(fd);
}

int OBSERVER(fclose)(FILE *stream) {
  note_close(fileno(stream));
  inside_stream_call++;
  const int result = REAL(fclose)(stream);
  inside_stream_call--;
  return result;
}

#ifndef __APPLE__
/* glibc gives C++ streams and fortified builds their own entry points. */
int open64(const char *path, int flags, ...) {
  va_list arguments;
  va_start(arguments, flags);
  const int mode = (flags & O_CREAT) ? va_arg(arguments, int) : 0;
  va_end(arguments);
  return open(path, flags, mode);
}
int __open_2(const char *path, int flags) { return open(path, flags); }
int __open64_2(const char *path, int flags) { return open(path, flags); }
FILE *fopen64(const char *path, const char *mode) { return fopen(path, mode); }
ssize_t pread64(int fd, void *buffer, size_t count, off_t offset) {
  return pread(fd, buffer, count, offset);
}
ssize_t __read_chk(int fd, void *buffer, size_t count, size_t limit) {
  (void)limit;
  return read(fd, buffer, count);
}
ssize_t __pread_chk(int fd, void *buffer, size_t count, off_t offset,
                    size_t limit) {
  (void)limit;
  return pread(fd, buffer, count, offset);
}
ssize_t __pread64_chk(int fd, void *buffer, size_t count, off_t offset,
                      size_t limit) {
  (void)limit;
  return pread(fd, buffer, count, offset);
}
size_t __fread_chk(void *buffer, size_t limit, size_t size, size_t count,
                   FILE *stream) {
  (void)limit;
  return fread(buffer, size, count, stream);
}
#endif

#ifdef __APPLE__
#define INTERPOSE(name)                                                     \
  __attribute__((used, section("__DATA,__interpose"))) static struct {      \
    const void *replacement, *original;                                     \
  } interpose_##name = {(const void *)observed_##name, (const void *)name}
INTERPOSE(open);
INTERPOSE(fopen);
INTERPOSE(read);
INTERPOSE(pread);
INTERPOSE(fread);
INTERPOSE(close);
INTERPOSE(fclose);
#endif

static void write_string(FILE *out, const char *text) {
  fputc('"', out);
  for (; *text; text++) {
    const unsigned char c = (unsigned char)*text;
    if (c == '"' || c == '\\') fprintf(out, "\\%c", c);
    else if (c < 0x20) fprintf(out, "\\u%04x", c);
    else fputc(c, out);
  }
  fputc('"', out);
}

__attribute__((constructor)) static void start_observing(void) {
#ifndef __APPLE__
  real_open = dlsym(RTLD_NEXT, "open");
  real_fopen = dlsym(RTLD_NEXT, "fopen");
  real_read = dlsym(RTLD_NEXT, "read");
  real_pread = dlsym(RTLD_NEXT, "pread");
  real_fread = dlsym(RTLD_NEXT, "fread");
  real_close = dlsym(RTLD_NEXT, "close");
  real_fclose = dlsym(RTLD_NEXT, "fclose");
#endif
  const char *out = getenv("SIL_HASH_TRACE_OUT");
  if (!out || strlen(out) >= sizeof out_path) return;
  strcpy(out_path, out);
  unsetenv("SIL_HASH_TRACE_OUT");
  unsetenv("DYLD_INSERT_LIBRARIES");
  unsetenv("LD_PRELOAD");
  owner = getpid();
  process_start_ns = now_ns();
  enabled = 1;
}

__attribute__((destructor)) static void write_trace(void) {
  if (!observing()) return;
  const uint64_t process_end_ns = now_ns();
  enabled = 0;
  FILE *out = REAL(fopen)(out_path, "w");
  if (!out) return;
  fprintf(out,
          "{\"process_start_ns\":%llu,\"process_end_ns\":%llu,"
          "\"dropped_files\":%llu,\"files\":[",
          (unsigned long long)process_start_ns,
          (unsigned long long)process_end_ns,
          (unsigned long long)dropped_files);
  for (int i = 0; i < file_count; i++) {
    const FileRecord *file = &files[i];
    fprintf(out, "%s{\"path\":", i ? "," : "");
    write_string(out, file->path);
    fprintf(out,
            ",\"opens\":%llu,\"reads\":%llu,\"bytes\":%llu,"
            "\"dropped_spans\":%llu,\"spans\":[",
            (unsigned long long)file->opens, (unsigned long long)file->reads,
            (unsigned long long)file->bytes,
            (unsigned long long)file->dropped_spans);
    for (int s = 0; s < file->span_count; s++) {
      const Span *span = &file->spans[s];
      fprintf(out,
              "%s{\"offset\":%llu,\"bytes\":%llu,\"size\":%llu,"
              "\"start_ns\":%llu,\"end_ns\":%llu}",
              s ? "," : "", (unsigned long long)span->offset,
              (unsigned long long)span->bytes, (unsigned long long)span->size,
              (unsigned long long)span->start_ns,
              (unsigned long long)span->end_ns);
    }
    fprintf(out, "]}");
  }
  fprintf(out, "]}\n");
  REAL(fclose)(out);
}
