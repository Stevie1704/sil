#include "run_signal.hpp"

#include <signal.h>

namespace {

volatile sig_atomic_t g_interrupt_signal = 0;

void record_interrupt(int signal_number) {
  if (g_interrupt_signal == 0) g_interrupt_signal = signal_number;
}

bool install(int signal_number, struct sigaction &action) {
  return sigaction(signal_number, &action, nullptr) == 0;
}

}  // namespace

namespace sil {

bool install_run_signal_handlers() noexcept {
  struct sigaction action {};
  action.sa_handler = record_interrupt;
  sigemptyset(&action.sa_mask);
  action.sa_flags = 0;  // Interrupt blocking protocol reads and polls.

  bool installed = install(SIGINT, action);
#ifdef SIGHUP
  installed = install(SIGHUP, action) && installed;
#endif
#ifdef SIGTERM
  installed = install(SIGTERM, action) && installed;
#endif
  return installed;
}

bool run_interrupted() noexcept { return g_interrupt_signal != 0; }

const char *run_interrupt_name() noexcept {
  switch (g_interrupt_signal) {
    case SIGINT:
      return "SIGINT";
#ifdef SIGHUP
    case SIGHUP:
      return "SIGHUP";
#endif
#ifdef SIGTERM
    case SIGTERM:
      return "SIGTERM";
#endif
    default:
      return "termination signal";
  }
}

std::string run_interrupt_message() {
  return std::string("run interrupted by ") + run_interrupt_name();
}

}  // namespace sil
