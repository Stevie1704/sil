/* Thread probe for the clock shim's thread policy (issue #262): starts one
 * worker through the dynamically linked pthread_create, joins it when it
 * started, and prints the observations as `key=value` lines on stdout.
 *
 *   rc          pthread_create's return code (0, or an error number)
 *   errno       errno after the call, which reject must leave alone
 *   worker_ran  1 when the worker body executed
 *
 * The shim's report line goes to stderr, so stdout stays the probe's own. */
#include <errno.h>
#include <pthread.h>
#include <stdio.h>

static int worker_ran = 0;

static void *worker(void *arg) {
    (void)arg;
    worker_ran = 1;
    return NULL;
}

int main(void) {
    pthread_t thread;
    errno = 0;
    int rc = pthread_create(&thread, NULL, worker, NULL);
    int err = errno;
    if (rc == 0)
        pthread_join(thread, NULL);
    printf("rc=%d\n", rc);
    printf("errno=%d\n", err);
    printf("worker_ran=%d\n", worker_ran);
    return 0;
}
