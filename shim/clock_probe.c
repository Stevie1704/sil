/* Clock-shim probe: exercises every interposed POSIX clock API and prints each
 * observable result as a `key=value` line. Run under the preload against a
 * test-controlled time region, it lets the unit tests assert the shim's
 * behavior end-to-end with no kernel and no manifest involved.
 *
 * All times are printed in nanoseconds. Sleep calls print their return value,
 * their errno and their remaining-time output, so one probe run covers both
 * sleep policies (issue #52) and the test asserts which one the region
 * selected. No sleep call may block under either policy.
 *
 * Clock IDs the shim does not virtualize — the CPU-time IDs and an
 * unrecognized one (issue #76) — are printed too, so the test can assert the
 * shim stepped aside rather than answering from the region. */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

static uint64_t ts_ns(const struct timespec *ts) {
    return (uint64_t)ts->tv_sec * 1000000000ull + (uint64_t)ts->tv_nsec;
}

static void print_clock(const char *key, clockid_t id) {
    struct timespec ts;
    if (clock_gettime(id, &ts) != 0) {
        printf("%s=error\n", key);
        return;
    }
    printf("%s=%llu\n", key, (unsigned long long)ts_ns(&ts));
}

/* Burn a fixed amount of CPU. Counted in iterations, not measured against a
 * clock: a virtualized clock never advances, so a loop that waited for one to
 * move would never end. `volatile` keeps the compiler from deleting it. */
static void burn_cpu(void) {
    volatile uint64_t sink = 0;
    for (uint64_t i = 0; i < 20000000ull; i++)
        sink += i;
}

/* Read a CPU-time clock, do work, read it again. A passed-through CPU clock
 * counts the work; a virtualized one is frozen for the whole step and reports
 * the same value twice. */
static void print_cpu_clock_pair(const char *key, clockid_t id) {
    struct timespec before, after;
    if (clock_gettime(id, &before) != 0) {
        printf("%s=error\n", key);
        return;
    }
    burn_cpu();
    if (clock_gettime(id, &after) != 0) {
        printf("%s=error\n", key);
        return;
    }
    printf("%s_before=%llu\n", key, (unsigned long long)ts_ns(&before));
    printf("%s_after=%llu\n", key, (unsigned long long)ts_ns(&after));
}

int main(void) {
    /* Monotonic-class: virtual t only. */
    print_clock("monotonic", CLOCK_MONOTONIC);
    print_clock("monotonic_raw", CLOCK_MONOTONIC_RAW);
#ifdef CLOCK_BOOTTIME
    print_clock("boottime", CLOCK_BOOTTIME);
#endif

    /* Realtime-class: epoch + virtual t. */
    print_clock("realtime", CLOCK_REALTIME);
#ifdef CLOCK_REALTIME_COARSE
    print_clock("realtime_coarse", CLOCK_REALTIME_COARSE);
#endif
#ifdef CLOCK_MONOTONIC_COARSE
    print_clock("monotonic_coarse", CLOCK_MONOTONIC_COARSE);
#endif

    /* An unrecognized clock ID has no class, so it passes through and the real
     * libc rejects it. Virtualizing it would answer a question the shim cannot
     * know the answer to. */
    print_clock("unknown", (clockid_t)9999);

    /* CPU-time IDs pass through (issue #76): they measure consumed CPU, not
     * wall time, so they keep running while virtual time is frozen. Printed
     * twice around a fixed amount of work, so the test can assert that the
     * second read is larger — the property no virtualized clock has.
     *
     * The busy loop is bounded by an iteration count, never by a clock read:
     * a virtualized clock would never advance and the loop would never end. */
#ifdef CLOCK_PROCESS_CPUTIME_ID
    print_cpu_clock_pair("process_cpu", CLOCK_PROCESS_CPUTIME_ID);
#else
    printf("process_cpu=unavailable\n");
#endif
#ifdef CLOCK_THREAD_CPUTIME_ID
    print_cpu_clock_pair("thread_cpu", CLOCK_THREAD_CPUTIME_ID);
#else
    printf("thread_cpu=unavailable\n");
#endif

    /* clock_getres reports 1 ns for virtualized IDs. */
    struct timespec res;
    if (clock_getres(CLOCK_MONOTONIC, &res) == 0)
        printf("getres=%llu\n", (unsigned long long)ts_ns(&res));
    else
        printf("getres=error\n");

    /* clock_getres for a CPU-time ID passes through, so it reports whatever
     * the real libc does rather than the shim's 1 ns. */
#ifdef CLOCK_PROCESS_CPUTIME_ID
    if (clock_getres(CLOCK_PROCESS_CPUTIME_ID, &res) == 0)
        printf("getres_cpu=%llu\n", (unsigned long long)ts_ns(&res));
    else
        printf("getres_cpu=error\n");
#else
    printf("getres_cpu=unavailable\n");
#endif

    /* gettimeofday: epoch + virtual t, in microseconds. */
    struct timeval tv;
    if (gettimeofday(&tv, NULL) == 0)
        printf("gettimeofday=%llu\n",
               (unsigned long long)((uint64_t)tv.tv_sec * 1000000000ull +
                                    (uint64_t)tv.tv_usec * 1000ull));
    else
        printf("gettimeofday=error\n");

    /* time: epoch + virtual t, in seconds. */
    time_t tsec = time(NULL);
    printf("time=%llu\n", (unsigned long long)((uint64_t)tsec * 1000000000ull));

    /* Frozen-step: a second read of the same clock must equal the first. */
    struct timespec a, b;
    clock_gettime(CLOCK_MONOTONIC, &a);
    clock_gettime(CLOCK_MONOTONIC, &b);
    printf("frozen=%d\n", ts_ns(&a) == ts_ns(&b) ? 1 : 0);

    /* Sleep family (issue #52). Never blocks under either policy; the test
     * asserts which return value and errno each policy produces. Each call
     * prints its result, its errno, and what it left in `rem`. */
    struct timespec req = {1, 500000000}; /* 1.5 s — must not actually block */
    struct timespec rem = {0, 0};

    errno = 0;
    int rc = nanosleep(&req, &rem);
    printf("nanosleep=%d\n", rc);
    printf("nanosleep_errno=%d\n", errno);
    printf("nanosleep_rem=%llu\n", (unsigned long long)ts_ns(&rem));

#ifdef __linux__
    /* clock_nanosleep is POSIX but the macOS libc does not provide it, so the
     * shim has nothing to interpose there and the probe skips it. It returns
     * the error number directly instead of setting errno. */
    rem = (struct timespec){0, 0};
    errno = 0;
    printf("clock_nanosleep=%d\n",
           clock_nanosleep(CLOCK_MONOTONIC, 0, &req, &rem));
    printf("clock_nanosleep_errno=%d\n", errno);
    printf("clock_nanosleep_rem=%llu\n", (unsigned long long)ts_ns(&rem));

    /* Absolute sleep: POSIX ignores `rem`, so the shim must leave it alone.
     * Seed it with a value the shim would have to overwrite to fail this. */
    struct timespec abs_req;
    clock_gettime(CLOCK_MONOTONIC, &abs_req);
    abs_req.tv_sec += 2;
    rem = (struct timespec){7, 0};
    printf("clock_nanosleep_abs=%d\n",
           clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &abs_req, &rem));
    printf("clock_nanosleep_abs_rem=%llu\n", (unsigned long long)ts_ns(&rem));

    /* A CPU-time clock is not virtualized, so this must reach the real libc.
     *
     * CLOCK_THREAD_CPUTIME_ID, which Linux documents as EINVAL for this call,
     * is deliberate: it returns immediately. Asking to sleep on a *process*
     * CPU clock would hang forever — a process blocked in the call burns no
     * CPU, so the deadline it is waiting for never arrives. What matters here
     * is only that the shim stepped aside, and an error from libc proves that
     * as well as a success would. */
    struct timespec cpu_req = {0, 1000000};
    printf("clock_nanosleep_cpu=%d\n",
           clock_nanosleep(CLOCK_THREAD_CPUTIME_ID, 0, &cpu_req, NULL));
#endif

    errno = 0;
    printf("usleep=%d\n", usleep(1500000));
    printf("usleep_errno=%d\n", errno);

    errno = 0;
    printf("sleep=%u\n", sleep(2));
    printf("sleep_errno=%d\n", errno);

    /* Time did not advance across the sleeps. */
    struct timespec after;
    clock_gettime(CLOCK_MONOTONIC, &after);
    printf("after_sleep=%llu\n", (unsigned long long)ts_ns(&after));

    /* Retry loop: the pattern the reject policy exists to end. A caller that
     * sleeps until a deadline passes cannot make progress while virtual time
     * is frozen, so under `immediate` this spins until the cap and under
     * `reject` it leaves on the first failed call. The cap keeps a regression
     * bounded instead of hanging the suite. */
    const int cap = 1000;
    int iterations = 0;
    while (iterations < cap) {
        struct timespec backoff = {0, 1000000}; /* 1 ms */
        iterations++;
        if (nanosleep(&backoff, NULL) != 0)
            break;
    }
    printf("retry_iterations=%d\n", iterations);
    printf("retry_capped=%d\n", iterations == cap ? 1 : 0);

    return 0;
}
