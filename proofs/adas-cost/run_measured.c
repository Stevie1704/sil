/* Runs one command and reports what its process tree cost.
 *
 *     run_measured REPORT COMMAND [ARG...]
 *
 * writes "<exit> <wall_ns> <user_us> <system_us> <max_rss>" into REPORT;
 * max_rss is in the unit of ru_maxrss (kilobytes on Linux, bytes on macOS).
 * Its own exit status is 0 when the report was written.
 *
 * Why a C launcher: on Linux, ru_maxrss survives execve. A Run started
 * directly from the measurement's Python driver reports at least the
 * driver's own peak RSS, whatever the Run uses. This launcher is small
 * when it forks, so the reported peak is the Run's own: the largest
 * process of its tree. The wall-clock interval is fork to reap.
 */
#define _DEFAULT_SOURCE

#include <stdio.h>
#include <sys/resource.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static long long now_ns(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static long long micros(struct timeval t) {
  return (long long)t.tv_sec * 1000000LL + t.tv_usec;
}

int main(int argc, char **argv) {
  if (argc < 3) {
    fprintf(stderr, "usage: run_measured REPORT COMMAND [ARG...]\n");
    return 2;
  }
  long long start = now_ns();
  pid_t pid = fork();
  if (pid < 0) {
    perror("run_measured: fork");
    return 2;
  }
  if (pid == 0) {
    execvp(argv[2], argv + 2);
    perror("run_measured: exec");
    _exit(127);
  }
  int status;
  struct rusage usage;
  if (wait4(pid, &status, 0, &usage) < 0) {
    perror("run_measured: wait4");
    return 2;
  }
  long long wall = now_ns() - start;
  int code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
  FILE *report = fopen(argv[1], "w");
  if (!report) {
    perror("run_measured: report");
    return 2;
  }
  fprintf(report, "%d %lld %lld %lld %ld\n", code, wall,
          micros(usage.ru_utime), micros(usage.ru_stime), usage.ru_maxrss);
  return fclose(report) == 0 ? 0 : 2;
}
