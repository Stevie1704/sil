/* sil-diagnostics-launcher: start the diagnostics runner from a small process.
 *
 *     sil-diagnostics-launcher [--status-fd N] RUNNER [ARG...]
 *
 * Runs RUNNER with ARG... as its own child and ends as RUNNER ended: with its
 * exit code, or by the signal that ended it. SIGINT, SIGTERM and SIGHUP sent
 * to the launcher are forwarded to RUNNER. The launcher writes nothing on
 * success; its own failures are one line and exit 2 (usage), 1 (fork or wait
 * failed) or 127 (RUNNER could not be executed).
 *
 * The failure line goes to stderr, or to the open file descriptor N with
 * --status-fd. RUNNER does not inherit N, so nothing the Run writes can
 * appear there: a supervisor reads N to tell a launcher failure from a Run
 * whose stderr happens to contain the same text.
 *
 * Why: on Linux, ru_maxrss survives execve. A runner forked directly from a
 * large Python supervisor reports at least the supervisor's resident size as
 * its own peak. This launcher is small when it forks, so the runner's peak
 * RSS is its own (#301, after proofs/adas-cost/run_measured.c). It sets no
 * LD_PRELOAD and changes nothing else in the runner's environment.
 */
#define _DEFAULT_SOURCE
#define _DARWIN_C_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static const int kForwarded[] = {SIGINT, SIGTERM, SIGHUP};
enum { kForwardedCount = sizeof kForwarded / sizeof *kForwarded };

static volatile sig_atomic_t runner_pid = 0;

static void forward(int signal_number) {
  if (runner_pid > 0) kill((pid_t)runner_pid, signal_number);
}

static void usage(void) {
  fprintf(stderr,
          "usage: sil-diagnostics-launcher [--status-fd N] RUNNER [ARG...]\n");
}

/* An open descriptor, marked close-on-exec so RUNNER never inherits it. */
static int parse_status_fd(const char *text) {
  char *end;
  errno = 0;
  const long value = strtol(text, &end, 10);
  if (errno != 0 || *text == '\0' || *end != '\0' || value < 0 ||
      value > INT_MAX)
    return -1;
  if (fcntl((int)value, F_SETFD, FD_CLOEXEC) == -1) return -1;
  return (int)value;
}

/* Ends this process the way the runner ended. */
static int end_like(int status) {
  if (WIFEXITED(status)) return WEXITSTATUS(status);
  const int signal_number = WTERMSIG(status);
  signal(signal_number, SIG_DFL);
  sigset_t only;
  sigemptyset(&only);
  sigaddset(&only, signal_number);
  sigprocmask(SIG_UNBLOCK, &only, NULL);
  raise(signal_number);
  return 128 + signal_number; /* the signal does not end the launcher */
}

int main(int argc, char **argv) {
  int status_fd = STDERR_FILENO;
  int first = 1;
  if (argc >= 2 && strcmp(argv[1], "--status-fd") == 0) {
    status_fd = argc >= 3 ? parse_status_fd(argv[2]) : -1;
    if (status_fd < 0) {
      usage();
      return 2;
    }
    first = 3;
  }
  if (argc <= first) {
    usage();
    return 2;
  }

  /* Block the forwarded signals until the runner's pid is known, so none
   * that arrives around fork is lost. */
  sigset_t forwarded, previous;
  sigemptyset(&forwarded);
  for (int i = 0; i < kForwardedCount; i++)
    sigaddset(&forwarded, kForwarded[i]);
  sigprocmask(SIG_BLOCK, &forwarded, &previous);

  struct sigaction action;
  memset(&action, 0, sizeof action);
  action.sa_handler = forward;
  sigemptyset(&action.sa_mask);
  for (int i = 0; i < kForwardedCount; i++)
    sigaction(kForwarded[i], &action, NULL);

  const pid_t pid = fork();
  if (pid < 0) {
    dprintf(status_fd, "sil-diagnostics-launcher: fork failed: %s\n",
            strerror(errno));
    return 1;
  }
  if (pid == 0) {
    /* exec resets the handlers; the runner gets the supervisor's mask. */
    sigprocmask(SIG_SETMASK, &previous, NULL);
    execvp(argv[first], argv + first);
    dprintf(status_fd, "sil-diagnostics-launcher: cannot run '%s': %s\n",
            argv[first], strerror(errno));
    _exit(127);
  }
  runner_pid = pid;
  sigprocmask(SIG_SETMASK, &previous, NULL);

  int status;
  for (;;) {
    if (waitpid(pid, &status, 0) == pid) break;
    if (errno == EINTR) continue;
    dprintf(status_fd, "sil-diagnostics-launcher: wait failed: %s\n",
            strerror(errno));
    return 1;
  }
  return end_like(status);
}
