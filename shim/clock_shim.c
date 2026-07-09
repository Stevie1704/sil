/* SiL virtual clock shim — a self-contained preload library that serves
 * virtual time from a shared region, so opaque POSIX process participants that
 * read the wall clock themselves see stepped virtual time instead.
 *
 * It knows nothing about the step protocol, manifest, or kernel: at load it
 * maps the fixed-layout region — a small memory-mapped file whose path is named
 * by SIL_CLOCK_REGION — and thereafter answers every interposed clock read
 * directly from that mapping (no per-read syscall). Reads are frozen within a
 * step: the region's `t` only changes when the writer advances it between steps.
 * (A plain mmap'd file is used rather than POSIX shm for portability — macOS
 * shm_open rejects reopening an object by name with EACCES.)
 *
 *   Linux:  built as an LD_PRELOAD library; the exported symbols shadow libc.
 *   macOS:  built with a __DATA,__interpose table (DYLD_INTERPOSE) and loaded
 *           via DYLD_INSERT_LIBRARIES.
 *
 * If the region is absent or unmappable the interposers fall back to the real
 * libc functions, so a mis-set environment degrades to real time rather than
 * crashing the process. */
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "sil/clock_region.h"

/* --- region mapping -------------------------------------------------------- */

static const volatile sil_clock_region *g_region = NULL;

/* Map the shared region named by the environment. Runs once at library load
 * (constructor) so the hot path is a plain pointer read. */
__attribute__((constructor)) static void sil_clock_shim_init(void) {
    const char *path = getenv(SIL_CLOCK_REGION_ENV);
    if (!path || !*path)
        return;
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return;
    void *p = mmap(NULL, sizeof(sil_clock_region), PROT_READ, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED)
        return;
    g_region = (const volatile sil_clock_region *)p;
}

/* True when a monotonic-class clock ID (virtual t only, no epoch). */
static int is_monotonic(clockid_t id) {
    switch (id) {
        case CLOCK_MONOTONIC:
        case CLOCK_MONOTONIC_RAW:
#ifdef CLOCK_BOOTTIME
        case CLOCK_BOOTTIME:
#endif
            return 1;
        default:
            return 0;
    }
}

/* Virtual nanoseconds for a clock ID: t for monotonic-class, epoch + t for
 * realtime-class. */
static uint64_t virtual_ns(clockid_t id) {
    uint64_t t = g_region->t;
    return is_monotonic(id) ? t : g_region->epoch + t;
}

static void fill_timespec(struct timespec *ts, uint64_t ns) {
    ts->tv_sec = (time_t)(ns / 1000000000ull);
    ts->tv_nsec = (long)(ns % 1000000000ull);
}

/* --- interposed implementations -------------------------------------------- */
/* Each interposer serves virtual time when the region is mapped and otherwise
 * falls back to the real libc function. On macOS the replacement is a distinct
 * symbol wired up by the __interpose table, so it can call the libc function by
 * its normal name. On Linux the shim's definitions shadow libc under
 * LD_PRELOAD, so the real symbol is reached via dlsym(RTLD_NEXT, ...). */

#if defined(__APPLE__)

int sil_clock_gettime(clockid_t id, struct timespec *ts) {
    if (!g_region)
        return clock_gettime(id, ts);
    fill_timespec(ts, virtual_ns(id));
    return 0;
}

int sil_clock_getres(clockid_t id, struct timespec *res) {
    if (!g_region)
        return clock_getres(id, res);
    if (res) {
        res->tv_sec = 0;
        res->tv_nsec = 1; /* 1 ns resolution */
    }
    return 0;
}

int sil_gettimeofday(struct timeval *tv, void *tz) {
    if (!g_region)
        return gettimeofday(tv, tz);
    uint64_t ns = virtual_ns(CLOCK_REALTIME);
    if (tv) {
        tv->tv_sec = (time_t)(ns / 1000000000ull);
        tv->tv_usec = (suseconds_t)((ns % 1000000000ull) / 1000ull);
    }
    return 0;
}

time_t sil_time(time_t *out) {
    if (!g_region)
        return time(out);
    time_t s = (time_t)(virtual_ns(CLOCK_REALTIME) / 1000000000ull);
    if (out)
        *out = s;
    return s;
}

int sil_nanosleep(const struct timespec *req, struct timespec *rem) {
    if (!g_region)
        return nanosleep(req, rem);
    if (rem) {
        rem->tv_sec = 0;
        rem->tv_nsec = 0;
    }
    return 0; /* immediate success, no blocking */
}

unsigned int sil_sleep(unsigned int seconds) {
    if (!g_region)
        return sleep(seconds);
    return 0; /* full duration "elapsed" instantly */
}

int sil_usleep(useconds_t usec) {
    if (!g_region)
        return usleep(usec);
    return 0;
}

/* DYLD interpose table: pairs (replacement, original). */
#define DYLD_INTERPOSE(_repl, _orig)                                         \
    __attribute__((used)) static struct {                                   \
        const void *repl;                                                   \
        const void *orig;                                                   \
    } _interpose_##_orig __attribute__((section("__DATA,__interpose"))) = {  \
        (const void *)&_repl, (const void *)&_orig}

DYLD_INTERPOSE(sil_clock_gettime, clock_gettime);
DYLD_INTERPOSE(sil_clock_getres, clock_getres);
DYLD_INTERPOSE(sil_gettimeofday, gettimeofday);
DYLD_INTERPOSE(sil_time, time);
DYLD_INTERPOSE(sil_nanosleep, nanosleep);
DYLD_INTERPOSE(sil_sleep, sleep);
DYLD_INTERPOSE(sil_usleep, usleep);

#else /* Linux: exported symbols shadow libc under LD_PRELOAD. */

#include <dlfcn.h>

/* Resolve the real libc symbol lazily for the region-absent fallback. */
#define REAL(name)                                                    \
    ({                                                                \
        static __typeof__(&name) _real = NULL;                        \
        if (!_real)                                                   \
            _real = (__typeof__(&name))dlsym(RTLD_NEXT, #name);       \
        _real;                                                        \
    })

int clock_gettime(clockid_t id, struct timespec *ts) {
    if (!g_region)
        return REAL(clock_gettime)(id, ts);
    fill_timespec(ts, virtual_ns(id));
    return 0;
}

int clock_getres(clockid_t id, struct timespec *res) {
    if (!g_region)
        return REAL(clock_getres)(id, res);
    if (res) {
        res->tv_sec = 0;
        res->tv_nsec = 1;
    }
    return 0;
}

int gettimeofday(struct timeval *tv, void *tz) {
    if (!g_region)
        return REAL(gettimeofday)(tv, tz);
    uint64_t ns = virtual_ns(CLOCK_REALTIME);
    if (tv) {
        tv->tv_sec = (time_t)(ns / 1000000000ull);
        tv->tv_usec = (suseconds_t)((ns % 1000000000ull) / 1000ull);
    }
    return 0;
}

time_t time(time_t *out) {
    if (!g_region)
        return REAL(time)(out);
    time_t s = (time_t)(virtual_ns(CLOCK_REALTIME) / 1000000000ull);
    if (out)
        *out = s;
    return s;
}

int nanosleep(const struct timespec *req, struct timespec *rem) {
    if (!g_region)
        return REAL(nanosleep)(req, rem);
    if (rem) {
        rem->tv_sec = 0;
        rem->tv_nsec = 0;
    }
    return 0;
}

int clock_nanosleep(clockid_t id, int flags, const struct timespec *req,
                    struct timespec *rem) {
    if (!g_region)
        return REAL(clock_nanosleep)(id, flags, req, rem);
    if (rem) {
        rem->tv_sec = 0;
        rem->tv_nsec = 0;
    }
    return 0;
}

unsigned int sleep(unsigned int seconds) {
    if (!g_region)
        return REAL(sleep)(seconds);
    return 0;
}

int usleep(useconds_t usec) {
    if (!g_region)
        return REAL(usleep)(usec);
    return 0;
}

#endif
