/* Clock-shim probe: exercises every interposed POSIX clock API and prints each
 * observable result as a `key=value` line. Run under the preload against a
 * test-controlled time region, it lets the unit tests assert the shim's
 * behavior end-to-end with no kernel and no manifest involved.
 *
 * All times are printed in nanoseconds. Sleep calls print their return value;
 * the test asserts they return success without blocking. */
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

int main(void) {
    /* Monotonic-class: virtual t only. */
    print_clock("monotonic", CLOCK_MONOTONIC);
    print_clock("monotonic_raw", CLOCK_MONOTONIC_RAW);
#ifdef CLOCK_BOOTTIME
    print_clock("boottime", CLOCK_BOOTTIME);
#endif

    /* Realtime-class: epoch + virtual t. */
    print_clock("realtime", CLOCK_REALTIME);

    /* clock_getres reports 1 ns. */
    struct timespec res;
    if (clock_getres(CLOCK_MONOTONIC, &res) == 0)
        printf("getres=%llu\n", (unsigned long long)ts_ns(&res));
    else
        printf("getres=error\n");

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

    /* Sleep family: must return success immediately without advancing time. */
    struct timespec req = {1, 500000000}; /* 1.5 s — must not actually block */
    struct timespec rem = {0, 0};
    printf("nanosleep=%d\n", nanosleep(&req, &rem));
#ifdef __linux__
    /* clock_nanosleep is POSIX but the macOS libc does not provide it, so the
     * shim has nothing to interpose there and the probe skips it. */
    printf("clock_nanosleep=%d\n",
           clock_nanosleep(CLOCK_MONOTONIC, 0, &req, &rem));
#endif
    printf("usleep=%d\n", usleep(1500000));
    printf("sleep=%u\n", sleep(2));

    /* Time did not advance across the sleeps. */
    struct timespec after;
    clock_gettime(CLOCK_MONOTONIC, &after);
    printf("after_sleep=%llu\n", (unsigned long long)ts_ns(&after));

    return 0;
}
