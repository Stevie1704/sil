/* Disposable #330 workload. The syscall wait keeps a short capture observable;
 * it is fixture pacing, never Participant Virtual time. */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

__attribute__((noinline)) uint64_t proof_alloc_site(unsigned seed) {
    uint64_t result = 0;
    for (unsigned i = 0; i < 8; ++i) {
        volatile unsigned char *p = malloc(2048);
        p[0] = (unsigned char)(seed + i);
        result += p[0];
        free((void *)p);
    }
    return result;
}

void proof_host_wait(void) {
    struct timespec delay = {0, 25000000};
    syscall(SYS_nanosleep, &delay, NULL);
}
