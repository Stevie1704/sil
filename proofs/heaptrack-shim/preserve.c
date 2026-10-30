/* Experiment only: force Heaptrack's lazy initialization, then restore only
 * the Clock shim for future execs. Preload this BEFORE Heaptrack so this
 * constructor runs after Heaptrack and the Clock shim constructors.
 * This does not exempt Heaptrack's threads, clocks or sleeps. */
#include <stdlib.h>

static void *volatile initialization_allocation;

__attribute__((constructor)) static void preserve_shim(void) {
    initialization_allocation = malloc(1);
    free(initialization_allocation);
    const char *shim = getenv("SILPROOF_SHIM_PRELOAD");
    if (!shim || setenv("LD_PRELOAD", shim, 1) != 0)
        _Exit(90);
}
