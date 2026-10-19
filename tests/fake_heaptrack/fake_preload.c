/* A stand-in for Heaptrack's preload library, for the `sil profile
 * --collector heap` orchestration tests (tests/test_profile_heap_run.py).
 *
 * It has the process behavior of libheaptrack_preload.so that the command
 * depends on: when it is loaded with DUMP_HEAPTRACK_OUTPUT set, it removes
 * LD_PRELOAD and DUMP_HEAPTRACK_OUTPUT from the environment (so children
 * are not traced), opens the output (a FIFO or a file), writes the header
 * and the traced command line at load, and the rest of its data at normal
 * process exit. A process that ends by a signal or _exit writes no rest. It
 * traces nothing: the rest is the file FAKE_HEAPTRACK_DATA (interpreted
 * format, without its first line), and the fake interpreter copies it.
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static FILE *output;

static void write_command_line(void) {
  char text[4096];
  FILE *cmdline = fopen("/proc/self/cmdline", "rb");
  if (!cmdline) return;
  size_t n = fread(text, 1, sizeof text - 1, cmdline);
  fclose(cmdline);
  while (n > 0 && text[n - 1] == '\0') n--;
  for (size_t i = 0; i < n; i++)
    if (text[i] == '\0') text[i] = ' ';
  text[n] = '\0';
  fprintf(output, "X %s\n", text);
}

static void finish(void) {
  const char *data = getenv("FAKE_HEAPTRACK_DATA");
  FILE *rest = data ? fopen(data, "rb") : NULL;
  if (rest) {
    char block[4096];
    size_t n;
    while ((n = fread(block, 1, sizeof block, rest)) > 0)
      fwrite(block, 1, n, output);
    fclose(rest);
  }
  fclose(output);
}

__attribute__((constructor)) static void start(void) {
  const char *path = getenv("DUMP_HEAPTRACK_OUTPUT");
  if (!path) return;
  char *copy = strdup(path);
  unsetenv("LD_PRELOAD");
  unsetenv("DUMP_HEAPTRACK_OUTPUT");
  output = fopen(copy, "w");
  free(copy);
  if (!output) return;
  fputs("v 10500 3\n", output);
  write_command_line();
  fflush(output);
  atexit(finish);
}
