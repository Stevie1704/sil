#include "failure_report.hpp"

#include <cerrno>
#include <cstring>
#include <fstream>
#include <stdexcept>

#include <nlohmann/json.hpp>

namespace sil {

namespace fs = std::filesystem;
using nlohmann::json;

namespace {

constexpr int kSchemaVersion = 1;

template <typename T>
json optional_json(const std::optional<T> &value) {
  return value ? json(*value) : json(nullptr);
}

}  // namespace

const char *failure_category_name(FailureCategory category) {
  switch (category) {
    case FailureCategory::Manifest:
      return "manifest";
    case FailureCategory::Spawn:
      return "spawn";
    case FailureCategory::Protocol:
      return "protocol";
    case FailureCategory::Timeout:
      return "timeout";
    case FailureCategory::Exit:
      return "exit";
    case FailureCategory::Guard:
      return "guard";
    case FailureCategory::Participant:
      return "participant";
    case FailureCategory::Interrupted:
      return "interrupted";
    case FailureCategory::Run:
      break;
  }
  return "run";
}

fs::path default_failure_report_path(const fs::path &recording_path) {
  return fs::path(recording_path.string() + ".failure.json");
}

void write_failure_report(const fs::path &path, int exit_code,
                          const std::string &message,
                          const FailureFacts &failure) {
  json document = {
      {"category", failure_category_name(failure.category)},
      {"exit_code", exit_code},
      {"exit_status", optional_json(failure.exit_status)},
      {"message", message},
      {"participant", failure.participant.empty() ? json(nullptr)
                                                  : json(failure.participant)},
      {"schema_version", kSchemaVersion},
      {"signal", optional_json(failure.signal)},
      {"stderr_tail", failure.stderr_tail},
      {"step_index", optional_json(failure.step_index)},
      {"virtual_time_ns", optional_json(failure.virtual_time_ns)},
  };

  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  if (!output)
    throw std::runtime_error("cannot open failure report '" + path.string() +
                             "' for writing: " + std::strerror(errno));
  // A Participant's stderr is not guaranteed to be UTF-8; replace what is not
  // rather than lose the whole report.
  output << document.dump(-1, ' ', false, json::error_handler_t::replace)
         << '\n';
  if (!output)
    throw std::runtime_error("cannot write failure report '" + path.string() +
                             "'");
}

}  // namespace sil
