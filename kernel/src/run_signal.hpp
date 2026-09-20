#pragma once

#include <string>

namespace sil {

// Installs the run-boundary termination handlers. The handlers only record
// the signal; the runner observes that state at safe points and performs the
// normal exception-driven cleanup path.
bool install_run_signal_handlers() noexcept;

bool run_interrupted() noexcept;
const char *run_interrupt_name() noexcept;
std::string run_interrupt_message();

}  // namespace sil
