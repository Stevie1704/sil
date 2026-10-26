#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
static uint64_t now(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) exit(2);
    return (uint64_t)t.tv_sec * 1000000000ull + t.tv_nsec;
}
__attribute__((noinline)) int proof_malloc_site(void) {
    for (unsigned i = 0; i < 50000; ++i) {
        void *p = malloc(2048);
        if (!p) return 2;
        ((volatile unsigned char *)p)[0] = (unsigned char)i;
        __asm__ volatile("" : : "r"(p) : "memory");
        free(p);
    }
    return 0;
}
int main(void) {
    uint64_t start = now();
    int rc = proof_malloc_site();
    printf("FIXTURE_NS %llu UID %u RC %d\n",
           (unsigned long long)(now() - start), (unsigned)getuid(), rc);
    return rc;
}
