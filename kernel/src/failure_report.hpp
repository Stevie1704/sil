#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

namespace sil {

// What went wrong in a failed Run, in the words the failure report uses.
// docs/running.md lists each value.
enum class FailureCategory {
  Manifest,     // rejected before any Participant was stepped (exit 2)
  Spawn,        // the kernel could not start a Process participant
  Protocol,     // a Participant broke the Step protocol
  Timeout,      // a Participant did not answer before its deadline
  Exit,         // a Participant exited or was killed by a signal
  Guard,        // a Participant exceeded a Run-boundary size guard
  Participant,  // a Participant reported its own failure
  Interrupted,  // sil-run received a termination signal
  Run,          // any other Run failure
};

// The facts about a failed Run that a diagnostic line cannot carry in a form
// an agent can parse. Absent fields are unknown or do not apply.
struct FailureFacts {
  FailureCategory category = FailureCategory::Run;
  std::string participant;
  std::optional<int> exit_status;
  std::optional<int> signal;
  // The virtual time and the 0-based index of the last Step sent to the
  // Participant. Both are absent when the failure came before its first Step.
  std::optional<uint64_t> virtual_time_ns;
  std::optional<uint64_t> step_index;
  std::vector<std::string> stderr_tail;
};

// The report sits beside the Recording, like the provenance side-car.
std::filesystem::path default_failure_report_path(
    const std::filesystem::path &recording_path);

void write_failure_report(const std::filesystem::path &path, int exit_code,
                          const std::string &message,
                          const FailureFacts &failure);

}  // namespace sil
