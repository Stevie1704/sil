/* A Native participant with source scopes, one scenario per Run (#372).
 *
 * Built with SIL_SOURCE_SCOPES=1 and linked to libsil_source_scope.so.1. The
 * config selects the scenario ("scenario"); each Activation of the Task
 * "tick" publishes one toy.Counter on "ticks", so the Recording has content.
 * The expected counts and reasons of each scenario are in
 * tests/test_source_scope_capture.py, from this source.
 *
 *   plain         scope 1 once per Activation
 *   recursion     scope 2 recursive, depth 3 (Q2)
 *   early_return  scope 3 around two calls of scope 4: one returns early,
 *                 one at its end (Q3)
 *   two_tasks     Tasks "tick" (scope 5) and "tock" (scope 6, twice) at the
 *                 same Virtual times (Q4)
 *   longjmp       scopes 7 and 8 nested, left by longjmp (Q5)
 *   worker        scope 9 on a worker thread, scope 10 on the runner (Q7)
 *   fail          scope 11, then api->fail in Activation 2 (Q8)
 *   interrupt     scope 11, then SIGINT and api->fail in Activation 2 (Q8)
 *   contention    two threads in scope 1 until the observer counts a
 *                 contended try-lock (Q9)
 *   kill          scope 1, then SIGKILL inside scope 1 in Activation 3 (Q14)
 *   slow          scope 1 and a 1 ms host sleep per Activation (Q14)
 *   corrupt       scope 1; in Activation 1, flags 1 in the raw header (Q13)
 *   fill          scope 1; "path" fills its file system in Activation
 *                 "fill_at" and is removed in Activation "last" (Q16)
 *
 * scope_scenarios_probe(depth) runs scope 2 recursively outside any Run:
 * a Process participant loads this library to call it (Q17).
 *
 * State is global: one Participant of this library per Run.
 */
#define _GNU_SOURCE

#include <fcntl.h>
#include <pthread.h>
#include <setjmp.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include <sil/participant.h>
#include <sil/source_scope.h>

#include "toy_messages.h"

static struct {
  const sil_api_v1 *api;
  char scenario[32];
  char path[512];
  uint64_t activations;  /* of "tick" */
  uint64_t fill_at;      /* "fill": the Activation that fills */
  uint64_t last;         /* "fill": the Activation that frees */
  jmp_buf jump;
} g;

/* The string value of "key" in the participant's compact JSON config. */
static void config_string(const char *json, const char *key, char *out,
                          size_t size) {
  char pattern[64];
  snprintf(pattern, sizeof pattern, "\"%s\":\"", key);
  const char *at = strstr(json, pattern);
  out[0] = '\0';
  if (at == NULL) return;
  at += strlen(pattern);
  size_t n = 0;
  while (at[n] != '\0' && at[n] != '"' && n + 1 < size) n++;
  memcpy(out, at, n);
  out[n] = '\0';
}

static int is(const char *scenario) { return strcmp(g.scenario, scenario) == 0; }

static void publish(void) {
  toy_Counter message = {g.activations, (int64_t)g.activations};
  g.api->publish(g.api->ctx, "ticks", &message, sizeof message);
}

static void plain(void) { SIL_SOURCE_SCOPE(1, "plain"); }

static int recurse(int depth) {
  SIL_SOURCE_SCOPE(2, "recurse");
  if (depth == 0) return 0;
  return 1 + recurse(depth - 1);
}

__attribute__((visibility("default"))) int scope_scenarios_probe(int depth) {
  return recurse(depth);
}

static __attribute__((noinline)) int inner(int early) {
  SIL_SOURCE_SCOPE(4, "inner");
  if (early) return 1;
  return 2;
}

static int outer(void) {
  SIL_SOURCE_SCOPE(3, "outer");
  return inner(1) + inner(0);
}

static __attribute__((noinline)) void jump_inner(void) {
  SIL_SOURCE_SCOPE(8, "jump_inner");
  longjmp(g.jump, 1);
}

static __attribute__((noinline)) void jump_outer(void) {
  SIL_SOURCE_SCOPE(7, "jump_outer");
  jump_inner();
}

static void *worker_scope(void *unused) {
  (void)unused;
  SIL_SOURCE_SCOPE(9, "worker");
  return NULL;
}

static void runner_scope(void) { SIL_SOURCE_SCOPE(10, "runner"); }

static void failing_scope(void) { SIL_SOURCE_SCOPE(11, "failing"); }

/* Contention: the raw header's dropped_contended, read through the session
 * descriptor. A test fixture only: a source owner never reads the file. */
static const volatile uint64_t *contended_counter(void) {
  const char *session = getenv("SIL_SOURCE_SCOPE_SESSION");
  const char *fd = session ? strstr(session, "fd=") : NULL;
  if (fd == NULL) return NULL;
  void *header = mmap(NULL, 256, PROT_READ, MAP_SHARED, atoi(fd + 3), 0);
  return header == MAP_FAILED ? NULL
                              : (const volatile uint64_t *)((char *)header + 64);
}

static atomic_int contending;

static void *contend(void *counter) {
  const volatile uint64_t *contended = counter;
  for (long i = 0; i < 20000000 && *contended == 0; i++) plain();
  atomic_store(&contending, 0);
  return NULL;
}

static void contention(void) {
  const volatile uint64_t *contended = contended_counter();
  if (contended == NULL) return;
  pthread_t thread;
  atomic_store(&contending, 1);
  pthread_create(&thread, NULL, contend, (void *)contended);
  while (atomic_load(&contending)) plain();
  pthread_join(thread, NULL);
}

static void corrupt_header(void) {
  const char *session = getenv("SIL_SOURCE_SCOPE_SESSION");
  const char *fd = session ? strstr(session, "fd=") : NULL;
  const uint32_t flags = 1;
  if (fd != NULL) (void)pwrite(atoi(fd + 3), &flags, sizeof flags, 36);
}

/* Fill the file system of `path`, leaving no block for other writers. */
static void fill(const char *path) {
  const int fd = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
  if (fd < 0) return;
  off_t size = 1 << 20;
  while (posix_fallocate(fd, 0, size) == 0) size *= 2;
  for (off_t step = size / 2; step >= 4096; step /= 2)
    if (posix_fallocate(fd, 0, size - step) == 0) break;
  for (char block[4096] = {1}; write(fd, block, sizeof block) > 0;) {
  }
  close(fd);
}

static void tick(void *user, uint64_t now_ns) {
  (void)user;
  (void)now_ns;
  const uint64_t index = g.activations;
  if (is("plain") || is("fill") || is("corrupt") || is("slow")) plain();
  if (is("slow")) usleep(1000);
  if (is("recursion")) recurse(2);
  if (is("early_return")) outer();
  if (is("two_tasks")) {
    SIL_SOURCE_SCOPE(5, "tick");
  }
  if (is("longjmp") && setjmp(g.jump) == 0) jump_outer();
  if (is("worker")) {
    pthread_t thread;
    pthread_create(&thread, NULL, worker_scope, NULL);
    pthread_join(thread, NULL);
    runner_scope();
  }
  if (is("fail") || is("interrupt")) {
    failing_scope();
    if (index == 2) {
      if (is("interrupt")) raise(SIGINT);
      g.api->fail(g.api->ctx, "scope scenario failure");
    }
  }
  if (is("contention") && index == 0) contention();
  if (is("kill") && index == 3) {
    SIL_SOURCE_SCOPE(1, "plain");
    raise(SIGKILL);
  } else if (is("kill")) {
    plain();
  }
  if (is("corrupt") && index == 1) corrupt_header();
  if (is("fill") && index == g.fill_at) fill(g.path);
  if (is("fill") && index == g.last) unlink(g.path);
  publish();
  g.activations++;
}

static void tock(void *user, uint64_t now_ns) {
  (void)user;
  (void)now_ns;
  for (int i = 0; i < 2; i++) {
    SIL_SOURCE_SCOPE(6, "tock");
  }
}

int sil_participant_init(const sil_api_v1 *api, const char *name,
                         const char *config_json) {
  (void)name;
  g.api = api;
  config_string(config_json, "scenario", g.scenario, sizeof g.scenario);
  config_string(config_json, "path", g.path, sizeof g.path);
  char last[32];
  config_string(config_json, "last", last, sizeof last);
  g.last = strtoull(last, NULL, 10);
  config_string(config_json, "fill_at", last, sizeof last);
  g.fill_at = strtoull(last, NULL, 10);
  const uint64_t period = 1000000;
  if (api->register_task(api->ctx, "tick", period, 0, 0, tick, NULL) != SIL_OK)
    return SIL_ERR;
  if (is("two_tasks"))
    return api->register_task(api->ctx, "tock", period, 0, 1, tock, NULL);
  return SIL_OK;
}
