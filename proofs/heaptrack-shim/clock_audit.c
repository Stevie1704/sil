/* Disposable clock_gettime audit. No constructors, allocation, or stdio in the
 * logging path: Heaptrack may call this before any constructor has run.
 * Each record is eight native little-endian uint64_t values. Repeated identical
 * values on a thread are suppressed; the hard bound is 1024 records. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static int (*next_clock)(clockid_t, struct timespec *);
static _Thread_local int resolving;
static _Thread_local uint64_t previous = UINT64_MAX;
static atomic_uint count;
static int output = -1;

static uint64_t host_ns(void) {
    struct timespec ts;
    syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

int clock_gettime(clockid_t id, struct timespec *ts) {
    if (resolving)
        return syscall(SYS_clock_gettime, id, ts);
    if (!next_clock) {
        resolving = 1;
        next_clock = dlsym(RTLD_NEXT, "clock_gettime");
        const char *path = getenv("SILPROOF_CLOCK_AUDIT");
        if (path)
            output = syscall(SYS_openat, AT_FDCWD, path,
                             O_WRONLY | O_CREAT | O_APPEND, 0600);
        resolving = 0;
    }
    uint64_t before = host_ns();
    int result = next_clock ? next_clock(id, ts)
                            : syscall(SYS_clock_gettime, id, ts);
    uint64_t after = host_ns();
    if (id == CLOCK_MONOTONIC && !result) {
        uint64_t value = (uint64_t)ts->tv_sec * 1000000000 + ts->tv_nsec;
        if (previous != value && output >= 0) {
            previous = value;
            unsigned index = atomic_fetch_add(&count, 1);
            if (index < 1024) {
                uint64_t row[] = {index, syscall(SYS_gettid), id,
                    (uintptr_t)__builtin_return_address(0), value, before,
                    after, result};
                syscall(SYS_write, output, row, sizeof(row));
            }
        }
    }
    return result;
}
