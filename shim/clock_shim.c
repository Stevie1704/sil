/* SiL virtual clock shim — a self-contained preload library that serves
 * virtual time from a shared region, so opaque POSIX process participants that
 * read the wall clock themselves see stepped virtual time instead.
 *
 * It knows nothing about the step protocol, manifest, or kernel: at load it
 * maps the fixed-layout region — a small memory-mapped file whose path is named
 * by SIL_CLOCK_REGION — and thereafter answers every *virtualized* clock read
 * directly from that mapping (no per-read syscall). Which clock IDs those are
 * is one table, below; the rest reach the real libc unchanged. Virtualized
 * reads are frozen within a step: the region's `t` only changes when the writer
 * advances it between steps.
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
#include <errno.h>
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

/* --- clock ID classification (issue #76) ------------------------------------
 * Every interposed call that takes a clock ID asks this one table which class
 * the ID is in, so no two of them can disagree about what is virtualized.
 *
 * Only wall-clock IDs are virtualized. A CPU-time ID measures consumed CPU,
 * not elapsed wall time, so freezing it would report the participant using no
 * CPU at all; it passes through to the real libc, as DESIGN.md records. An ID
 * the table does not name passes through for the same reason it cannot be
 * answered: an unknown clock has no known class, and guessing realtime would
 * hand a caller an epoch-based answer for a clock that may measure neither
 * wall time nor this process. */
typedef enum clock_class {
    CLASS_MONOTONIC,   /* virtual t, no epoch */
    CLASS_REALTIME,    /* epoch + virtual t */
    CLASS_PASSTHROUGH  /* not virtualized: the real libc answers */
} clock_class;

static clock_class classify(clockid_t id) {
    switch (id) {
        case CLOCK_MONOTONIC:
        case CLOCK_MONOTONIC_RAW:
#ifdef CLOCK_BOOTTIME
        case CLOCK_BOOTTIME:
#endif
#ifdef CLOCK_BOOTTIME_ALARM
        case CLOCK_BOOTTIME_ALARM:
#endif
#ifdef CLOCK_MONOTONIC_COARSE
        case CLOCK_MONOTONIC_COARSE:
#endif
#ifdef CLOCK_UPTIME_RAW
        case CLOCK_UPTIME_RAW:
#endif
        /* The cheap, less precise variants of the clocks above: same class,
         * and the region answers both at the same cost anyway. */
#ifdef CLOCK_MONOTONIC_RAW_APPROX
        case CLOCK_MONOTONIC_RAW_APPROX:
#endif
#ifdef CLOCK_UPTIME_RAW_APPROX
        case CLOCK_UPTIME_RAW_APPROX:
#endif
            return CLASS_MONOTONIC;
        case CLOCK_REALTIME:
#ifdef CLOCK_REALTIME_COARSE
        case CLOCK_REALTIME_COARSE:
#endif
#ifdef CLOCK_REALTIME_ALARM
        case CLOCK_REALTIME_ALARM:
#endif
        /* CLOCK_TAI reads as realtime. A Manifest declares one `epoch`, so the
         * model has no TAI-UTC offset to add; the leap-second difference is a
         * far smaller error than letting real time leak into a shimmed run. */
#ifdef CLOCK_TAI
        case CLOCK_TAI:
#endif
            return CLASS_REALTIME;
        default:
            return CLASS_PASSTHROUGH;
    }
}

/* True when the shim serves this clock ID from the region — which an unmapped
 * region never is, so this one predicate is the whole guard at every call. */
static int is_virtualized(clockid_t id) {
    return g_region && classify(id) != CLASS_PASSTHROUGH;
}

/* Virtual nanoseconds for a virtualized clock ID: t for monotonic-class,
 * epoch + t for realtime-class. */
static uint64_t virtual_ns(clockid_t id) {
    uint64_t t = g_region->t;
    return classify(id) == CLASS_MONOTONIC ? t : g_region->epoch + t;
}

static void fill_timespec(struct timespec *ts, uint64_t ns) {
    ts->tv_sec = (time_t)(ns / 1000000000ull);
    ts->tv_nsec = (long)(ns % 1000000000ull);
}

/* --- sleep family (issue #52) ----------------------------------------------
 * Virtual time is frozen for the whole step, so no policy can actually sleep.
 * `immediate` reports the full duration as elapsed, which is the recorded
 * pre-#52 behavior. `reject` fails the call instead.
 *
 * Reject fails with ENOSYS, not EINTR. EINTR is the one errno every correct
 * caller retries on, with the remaining time — which is exactly the spin this
 * policy exists to end. ENOSYS says the call is unavailable here, so a loop
 * that checks its errno leaves. */

/* True when the mapped region selects reject. An unmapped region never reaches
 * here (the interposers fall back to libc first), and an unrecognized policy
 * value reads as immediate. */
static int sleep_rejected(void) {
    return g_region && g_region->sleep_policy == SIL_SLEEP_REJECT;
}

/* Fill a relative sleep's `rem`. Immediate reports nothing remaining, because
 * the whole duration "elapsed"; reject reports all of it, because none did. */
static void fill_remaining(const struct timespec *req, struct timespec *rem,
                           int rejected) {
    if (!rem)
        return;
    if (rejected && req) {
        *rem = *req;
        return;
    }
    rem->tv_sec = 0;
    rem->tv_nsec = 0;
}

/* nanosleep and usleep convention: 0, or -1 with errno set. */
static int sleep_status(void) {
    if (!sleep_rejected())
        return 0;
    errno = ENOSYS;
    return -1;
}

/* sleep() returns seconds-left-unslept and has no errno channel in POSIX, so
 * reject cannot signal failure unambiguously here: it returns the full
 * duration as unslept and sets ENOSYS for callers that look. A caller that
 * ignores the return value cannot tell reject from immediate. nanosleep,
 * clock_nanosleep and usleep are the three a retry loop can act on. */
static unsigned int sleep_seconds_left(unsigned int seconds) {
    if (!sleep_rejected())
        return 0;
    errno = ENOSYS;
    return seconds;
}

/* --- interposed implementations -------------------------------------------- */
/* Each interposer serves virtual time when the region is mapped and otherwise
 * falls back to the real libc function. On macOS the replacement is a distinct
 * symbol wired up by the __interpose table, so it can call the libc function by
 * its normal name. On Linux the shim's definitions shadow libc under
 * LD_PRELOAD, so the real symbol is reached via dlsym(RTLD_NEXT, ...). */

#if defined(__APPLE__)

int sil_clock_gettime(clockid_t id, struct timespec *ts) {
    if (!is_virtualized(id))
        return clock_gettime(id, ts);
    fill_timespec(ts, virtual_ns(id));
    return 0;
}

int sil_clock_getres(clockid_t id, struct timespec *res) {
    if (!is_virtualized(id))
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
    fill_remaining(req, rem, sleep_rejected());
    return sleep_status();
}

unsigned int sil_sleep(unsigned int seconds) {
    if (!g_region)
        return sleep(seconds);
    return sleep_seconds_left(seconds);
}

int sil_usleep(useconds_t usec) {
    if (!g_region)
        return usleep(usec);
    return sleep_status();
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
    if (!is_virtualized(id))
        return REAL(clock_gettime)(id, ts);
    fill_timespec(ts, virtual_ns(id));
    return 0;
}

int clock_getres(clockid_t id, struct timespec *res) {
    if (!is_virtualized(id))
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
    fill_remaining(req, rem, sleep_rejected());
    return sleep_status();
}

int clock_nanosleep(clockid_t id, int flags, const struct timespec *req,
                    struct timespec *rem) {
    if (!is_virtualized(id))
        return REAL(clock_nanosleep)(id, flags, req, rem);
    /* POSIX makes clock_nanosleep the exception twice over: it returns the
     * error number rather than setting errno, and it ignores `rem` entirely
     * for an absolute sleep. */
    const int rejected = sleep_rejected();
    if (!(flags & TIMER_ABSTIME))
        fill_remaining(req, rem, rejected);
    return rejected ? ENOSYS : 0;
}

unsigned int sleep(unsigned int seconds) {
    if (!g_region)
        return REAL(sleep)(seconds);
    return sleep_seconds_left(seconds);
}

int usleep(useconds_t usec) {
    if (!g_region)
        return REAL(usleep)(usec);
    return sleep_status();
}

#endif
