/* Disposable Linux scheduler fixture. Publications never use host observations. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int markers = -1;
static const char *participant;
static atomic_int stop;
static pthread_t background;
static uint64_t now(void) {
    struct timespec t;
    if (syscall(SYS_clock_gettime, CLOCK_MONOTONIC, &t)) _exit(80);
    return (uint64_t)t.tv_sec * 1000000000ull + t.tv_nsec;
}
static void mark(const char *phase, const char *edge, uint64_t index, uint64_t vt) {
    char row[512];
    struct stat ns;
    if (stat("/proc/self/ns/pid", &ns)) _exit(81);
    int n = snprintf(row, sizeof row,
        "{\"host_ns\":%llu,\"pid\":%d,\"tid\":%ld,\"parent\":%d,"
        "\"namespace\":[%llu,%llu],\"participant\":\"%s\",\"phase\":\"%s\","
        "\"edge\":\"%s\",\"index\":%llu,\"virtual_ns\":%llu}\n",
        (unsigned long long)now(), getpid(), syscall(SYS_gettid), getppid(),
        (unsigned long long)ns.st_dev, (unsigned long long)ns.st_ino,
        participant, phase, edge, (unsigned long long)index, (unsigned long long)vt);
    if (n <= 0 || n >= (int)sizeof row || write(markers, row, n) != n) _exit(82);
}
static void sleep_ms(long ms) {
    struct timespec t = {ms / 1000, ms % 1000 * 1000000};
    while (nanosleep(&t, &t) && errno == EINTR) {}
}
static uint64_t work(unsigned count) {
    uint64_t x = 17;
    for (unsigned i = 0; i < count; ++i) {
        x = x * 6364136223846793005ull + 1442695040888963407ull;
        __asm__ volatile("" : "+r"(x));
    }
    return x;
}
static void *worker(void *unused) {
    (void)unused;
    mark("background", "begin", 0, 0);
    while (!atomic_load(&stop)) {
        work(1000000);
        sleep_ms(2);
    }
    mark("background", "end", 0, 0);
    return NULL;
}
void proof_open(const char *path, const char *name) {
    participant = name;
    markers = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (markers < 0) _exit(83);
    mark("lifetime", "begin", 0, 0);
    if (pthread_create(&background, NULL, worker, NULL)) _exit(84);
}
uint64_t proof_step(uint64_t index, uint64_t vt, const char *helper, int unclosed) {
    mark("step", "begin", index, vt);
    if (unclosed && index == 1) {
        atomic_store(&stop, 1);
        pthread_join(background, NULL);
        kill(getppid(), SIGKILL);
        _exit(9);
    }
    mark("cpu", "begin", index, vt);
    uint64_t value = work(15000000);
    mark("cpu", "end", index, vt);
    mark("sleep", "begin", index, vt);
    sleep_ms(20);
    mark("sleep", "end", index, vt);
    int pipefd[2];
    if (pipe(pipefd)) _exit(85);
    pid_t child = fork();
    if (child < 0) _exit(86);
    if (!child) {
        close(pipefd[0]);
        mark("wait_child", "begin", index, vt);
        sleep_ms(20);
        if (write(pipefd[1], "x", 1) != 1) _exit(87);
        mark("wait_child", "end", index, vt);
        _exit(0);
    }
    close(pipefd[1]);
    mark("wait", "begin", index, vt);
    char byte;
    if (read(pipefd[0], &byte, 1) != 1) _exit(88);
    mark("wait", "end", index, vt);
    close(pipefd[0]);
    if (waitpid(child, NULL, 0) != child) _exit(89);

    cpu_set_t saved, one;
    if (sched_getaffinity(0, sizeof saved, &saved)) _exit(90);
    int cpu = sched_getcpu();
    if (cpu < 0) _exit(91);
    CPU_ZERO(&one);
    CPU_SET(cpu, &one);
    if (sched_setaffinity(0, sizeof one, &one)) _exit(92);
    child = fork();
    if (child < 0) _exit(93);
    if (!child) {
        mark("competitor", "begin", index, vt);
        work(60000000);
        mark("competitor", "end", index, vt);
        _exit(0);
    }
    mark("contended", "begin", index, vt);
    value ^= work(30000000);
    mark("contended", "end", index, vt);
    if (waitpid(child, NULL, 0) != child ||
        sched_setaffinity(0, sizeof saved, &saved)) _exit(94);
    child = fork();
    if (child < 0) _exit(95);
    if (!child) {
        execl(helper, helper, "/proc/self/fd/0", (char *)NULL);
        _exit(96);
    }
    if (waitpid(child, NULL, 0) != child) _exit(97);
    mark("step", "end", index, vt);
    return value;
}
void proof_close(void) {
    atomic_store(&stop, 1);
    pthread_join(background, NULL);
    mark("lifetime", "end", 0, 0);
    close(markers);
}
int main(void) {
    /* A separately identified exec image, not a second protocol participant. */
    work(1000000);
    return 0;
}
