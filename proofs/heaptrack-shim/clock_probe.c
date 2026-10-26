/* C-only fixture: no clock_gettime calls of its own. Pace region transitions
 * with direct host syscalls, independently of the preloaded sleep policy. */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
#include "sil/clock_region.h"

static void *volatile allocation;

int main(void) {
    int fd = open(getenv("SILPROOF_CLOCK_REGION"), O_RDWR);
    if (fd < 0)
        return 80;
    volatile sil_clock_region *region = mmap(NULL, sizeof(*region),
        PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (region == MAP_FAILED)
        return 81;
    /* Save mappings after allocator initialization so caller addresses can be
     * resolved without dladdr allocating inside the clock audit. */
    allocation = malloc(2048);
    free(allocation);
    int maps = open("/proc/self/maps", O_RDONLY);
    int output = open(getenv("SILPROOF_CLOCK_MAPS"), O_WRONLY | O_CREAT, 0600);
    if (maps < 0 || output < 0)
        return 82;
    char buffer[4096];
    ssize_t size;
    while ((size = read(maps, buffer, sizeof(buffer))) > 0)
        if (write(output, buffer, size) != size)
            return 83;
    close(maps);
    close(output);
    const uint64_t values[] = {0, 100000000, 200000000, 400000000, 900000000};
    for (unsigned i = 0; i < sizeof(values) / sizeof(values[0]); ++i) {
        region->t = values[i];
        allocation = malloc(2048);
        free(allocation);
        struct timespec delay = {0, 20000000};
        syscall(SYS_nanosleep, &delay, NULL);
    }
    return 0;
}
