#include "provenance.hpp"

#include <sys/utsname.h>
#include <unistd.h>

#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif

#ifdef __GLIBC__
#include <gnu/libc-version.h>
#endif

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <cstdlib>
#include <fstream>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <utility>

#include <nlohmann/json.hpp>

#include "clock_shim.hpp"
#include "sha256.hpp"

namespace sil {

namespace {

using nlohmann::json;
namespace fs = std::filesystem;

struct MachineClass {
  std::string id;
  std::string os;
  std::string architecture;
  std::string libc;
};

struct ResolvedFile {
  fs::path path;
  std::string sha256;
};

struct ResolvedExecutable {
  // The path passed to execvp. Keeping a PATH symlink here preserves launcher
  // semantics such as Python virtual-environment discovery.
  fs::path launch_path;
  // The canonical target whose bytes are digested and reported.
  fs::path target_path;
};

std::string error_message(const std::error_code &error) {
  return error ? error.message() : "unknown filesystem error";
}

fs::path canonical_file(const fs::path &input, const fs::path &base) {
  if (input.empty()) throw std::runtime_error("path is empty");

  fs::path candidate = input;
  if (candidate.is_relative()) candidate = base / candidate;

  std::error_code error;
  fs::path resolved = fs::canonical(candidate, error);
  if (error)
    throw std::runtime_error("cannot resolve '" + candidate.string() + "': " +
                             error_message(error));
  if (!fs::is_regular_file(resolved, error)) {
    if (error)
      throw std::runtime_error("cannot inspect '" + resolved.string() +
                               "': " + error_message(error));
    throw std::runtime_error("'" + resolved.string() + "' is not a regular "
                             "file");
  }
  return resolved;
}

ResolvedFile resolve_and_digest(const fs::path &input, const fs::path &base) {
  ResolvedFile file;
  file.path = canonical_file(input, base);
  file.sha256 = sha256_file(file.path);
  return file;
}

std::string normalize_architecture(std::string architecture) {
  if (architecture == "x86_64" || architecture == "amd64")
    return "x86-64";
  if (architecture == "aarch64" || architecture == "arm64") return "arm64";
  return architecture;
}

MachineClass machine_class() {
  struct utsname info {};
  if (uname(&info) != 0)
    throw std::runtime_error("cannot identify machine class: " +
                             std::string(std::strerror(errno)));

  MachineClass result;
  result.os = info.sysname;
  result.architecture = normalize_architecture(info.machine);
#ifdef __GLIBC__
  result.libc = std::string("glibc-") + gnu_get_libc_version();
#elif defined(__APPLE__)
  result.libc = "libSystem";
#else
  result.libc = "unknown";
#endif
  result.id = result.os + "/" + result.architecture + "/" + result.libc;
  return result;
}

fs::path running_executable() {
#ifdef __APPLE__
  uint32_t size = 0;
  _NSGetExecutablePath(nullptr, &size);
  std::string buffer(size, '\0');
  if (_NSGetExecutablePath(buffer.data(), &size) != 0)
    throw std::runtime_error("cannot resolve the running executable path");
  return canonical_file(buffer.c_str(), {});
#else
  return canonical_file("/proc/self/exe", {});
#endif
}

bool executable_file(const fs::path &path) {
  return ::access(path.c_str(), X_OK) == 0;
}

ResolvedExecutable path_from_path_environment(
    const std::string &command, const fs::path &invocation_directory) {
  const char *environment = std::getenv("PATH");
  std::string path = environment ? environment : "";
  if (path.empty()) path = "/usr/local/bin:/usr/bin:/bin";

  size_t start = 0;
  for (;;) {
    const size_t separator = path.find(':', start);
    const std::string entry = path.substr(
        start, separator == std::string::npos ? std::string::npos
                                              : separator - start);
    fs::path directory = entry.empty() ? invocation_directory : fs::path(entry);
    if (directory.is_relative()) directory = invocation_directory / directory;
    fs::path candidate = directory / command;
    std::error_code error;
    if (fs::is_regular_file(candidate, error) && !error &&
        executable_file(candidate))
      return {candidate, canonical_file(candidate, {})};

    if (separator == std::string::npos) break;
    start = separator + 1;
  }
  throw std::runtime_error("executable '" + command +
                           "' was not found on PATH");
}

ResolvedExecutable resolve_executable(
    const std::string &command, const fs::path &invocation_directory) {
  if (command.empty()) throw std::runtime_error("executable name is empty");
  fs::path argument(command);
  if (argument.is_absolute() || argument.has_parent_path()) {
    fs::path launch_path = argument.is_absolute()
                               ? argument
                               : (invocation_directory / argument).lexically_normal();
    return {launch_path, canonical_file(launch_path, {})};
  }
  return path_from_path_environment(command, invocation_directory);
}

std::vector<std::string> resolve_command(
    const std::vector<std::string> &command,
    const fs::path &invocation_directory,
    const ResolvedExecutable &executable) {
  std::vector<std::string> resolved = command;
  resolved.at(0) = executable.launch_path.string();
  for (size_t index = 1; index < resolved.size(); ++index) {
    if (resolved[index].empty()) continue;
    fs::path argument(resolved[index]);
    if (argument.is_absolute()) continue;
    const fs::path candidate = invocation_directory / argument;
    std::error_code error;
    if (fs::exists(candidate, error) && !error)
      resolved[index] = candidate.lexically_normal().string();
  }
  return resolved;
}

// Every command argument that names a readable file is an artifact this Run
// loaded, so it is digested. The rule is deliberately about files rather than
// about any one foreign standard: an imported FMU archive is covered because it
// is a file the command names, and the kernel never learns FMI exists. An
// argument that names no file — a channel name, a flag, a numeric literal — is
// not an artifact and is left alone.
std::vector<std::string> command_file_arguments(
    const std::vector<std::string> &command, const fs::path &base) {
  std::vector<std::string> result;
  std::set<std::string> seen;
  // Index 0 is the executable, which is resolved and digested on its own.
  for (size_t index = 1; index < command.size(); ++index) {
    const std::string &argument = command[index];
    if (argument.empty()) continue;
    fs::path candidate(argument);
    if (candidate.is_relative()) candidate = base / candidate;
    std::error_code error;
    if (!fs::is_regular_file(candidate, error) || error) continue;
    if (seen.insert(argument).second) result.push_back(argument);
  }
  return result;
}

ProvenanceArtifact artifact(const ResolvedFile &file) {
  return {file.path.string(), file.sha256};
}

void add_error(Provenance &provenance, const std::string &name,
               const std::string &path, const std::string &reason) {
  provenance.errors.push_back({name, path, reason});
}

template <typename Body>
std::optional<ResolvedFile> collect_file(Provenance &provenance,
                                         const std::string &name,
                                         const std::string &display_path,
                                         Body body) {
  try {
    return body();
  } catch (const std::exception &error) {
    add_error(provenance, name, display_path, error.what());
    return std::nullopt;
  }
}

std::string preflight_error(const Provenance &provenance) {
  std::ostringstream message;
  message << "manifest error: artifact provenance preflight failed";
  for (const ProvenanceError &error : provenance.errors)
    message << "; " << error.artifact << " '" << error.path << "': "
            << error.reason;
  return message.str();
}

json artifact_json(const std::optional<ProvenanceArtifact> &value) {
  if (!value) return nullptr;
  return { {"path", value->path}, {"sha256", value->sha256} };
}

json artifact_json(const ProvenanceArtifact &value) {
  return { {"path", value.path}, {"sha256", value.sha256} };
}

}  // namespace

Provenance initialize_provenance(const Manifest &manifest,
                                 const std::string &sil_version,
                                 const std::string &source_repository,
                                 const std::string &source_revision) {
  Provenance provenance;
  provenance.manifest_hash = manifest.hash_hex;
  provenance.sil_version = sil_version;
  provenance.source_repository = source_repository;
  provenance.source_revision = source_revision;
  const MachineClass machine = machine_class();
  provenance.machine_class_id = machine.id;
  provenance.machine_os = machine.os;
  provenance.machine_architecture = machine.architecture;
  provenance.machine_libc = machine.libc;
  return provenance;
}

PreparedRun prepare_run(const Manifest &manifest, Provenance &provenance) {
  // The loader compiles these plans, and preparation is the only path to an
  // Engine. Keep the check here as well so a hand-built Manifest cannot forge
  // a ready Run by omitting a plan.
  for (const ChannelSpec &channel : manifest.channels)
    if (!channel.interceptor_plan)
      throw ManifestError("manifest error: channel '" + channel.name +
                          "': missing compiled interceptor plan");

  // Preserve the established precedence: a topology defect is reported before
  // artifact collection, and therefore before any participant can be loaded.
  validate_one_publisher_per_channel(manifest);

  // Every Manifest-named path resolves against the Manifest directory, so the
  // record describes the same artifacts from any working directory. Anchoring
  // to the invocation directory instead would put it in the record, and two
  // Runs of one Manifest from different directories would not be byte-identical.
  const fs::path &base = manifest.base_dir;
  std::vector<PreparedParticipantSpec> prepared_participants;
  prepared_participants.reserve(manifest.participants.size());

  if (const auto file = collect_file(
          provenance, "runner", "/proc/self/exe", [] {
            return resolve_and_digest(running_executable(), {});
          }))
    provenance.runner = artifact(*file);

  bool shim_requested = false;
  for (const ParticipantSpec &participant : manifest.participants) {
    if (const auto *process = std::get_if<ProcessSpec>(&participant.impl))
      shim_requested = shim_requested || process->shim;
  }
  if (shim_requested) {
    const fs::path shim = clock_shim_library_path();
    if (const auto file = collect_file(
            provenance, "Clock shim", shim.string(), [&shim] {
              return resolve_and_digest(shim, {});
            }))
      provenance.clock_shim = artifact(*file);
  }

  for (const ParticipantSpec &participant : manifest.participants) {
    if (auto *native = std::get_if<NativeSpec>(&participant.impl)) {
      ProvenanceNativeParticipant record{participant.name, std::nullopt};
      const fs::path requested = native->library;
      const fs::path launch_path =
          requested.is_absolute()
              ? requested
              : (manifest.base_dir / requested).lexically_normal();
      std::optional<fs::path> resolved_library;
      if (const auto file = collect_file(
              provenance, "Native participant '" + participant.name +
                              "' library",
              requested.string(), [&requested, &manifest] {
                return resolve_and_digest(requested, manifest.base_dir);
              })) {
        resolved_library = launch_path;
        record.library = artifact(*file);
      }
      provenance.native_participants.push_back(std::move(record));
      if (resolved_library)
        prepared_participants.push_back(
            {participant.name,
             PreparedNativeSpec{*native, std::move(*resolved_library)}});
      continue;
    }

    if (auto *process = std::get_if<ProcessSpec>(&participant.impl)) {
      ProvenanceProcessParticipant record{participant.name, process->command,
                                          std::nullopt};
      std::optional<ResolvedExecutable> executable;
      std::optional<std::vector<std::string>> resolved_command;
      try {
        executable = resolve_executable(process->command.at(0), base);
      } catch (const std::exception &error) {
        add_error(provenance,
                  "Process participant '" + participant.name +
                      "' executable",
                  process->command.empty() ? "" : process->command.front(),
                  error.what());
      }
      if (executable) {
        try {
          record.command = resolve_command(process->command, base, *executable);
          resolved_command = record.command;
        } catch (const std::exception &error) {
          add_error(provenance,
                    "Process participant '" + participant.name +
                        "' command",
                    process->command.empty() ? "" : process->command.front(),
                    error.what());
        }
        if (const auto file = collect_file(
                provenance,
                "Process participant '" + participant.name +
                    "' executable",
                executable->target_path.string(), [&executable] {
                  return resolve_and_digest(executable->target_path, {});
                }))
          record.executable = artifact(*file);
      }
      provenance.process_participants.push_back(std::move(record));

      for (const std::string &argument :
           command_file_arguments(process->command, base)) {
        ProvenanceCommandFile named{participant.name, std::nullopt};
        const fs::path requested = argument;
        if (const auto file = collect_file(
                provenance,
                "File named by participant '" + participant.name + "' command",
                requested.string(), [&requested, &base] {
                  return resolve_and_digest(requested, base);
                }))
          named.file = artifact(*file);
        provenance.command_files.push_back(std::move(named));
      }
      if (resolved_command)
        prepared_participants.push_back(
            {participant.name,
             PreparedProcessSpec{*process, std::move(*resolved_command)}});
      continue;
    }

    const auto *replay = std::get_if<ReplaySpec>(&participant.impl);
    prepared_participants.push_back({participant.name, *replay});
  }

  if (!provenance.errors.empty()) throw ManifestError(preflight_error(provenance));
  return PreparedRun(manifest, std::move(prepared_participants));
}

std::string sha256_file(const fs::path &path) {
  std::ifstream input(path, std::ios::binary);
  if (!input)
    throw std::runtime_error("cannot open '" + path.string() + "': " +
                             std::strerror(errno));

  Sha256 digest;
  char buffer[64 * 1024];
  while (input.read(buffer, sizeof buffer) || input.gcount() != 0)
    digest.update(buffer, static_cast<size_t>(input.gcount()));
  if (!input.eof())
    throw std::runtime_error("cannot read '" + path.string() + "'");
  return digest.hex_digest();
}

fs::path default_provenance_path(const fs::path &recording_path) {
  return fs::path(recording_path.string() + ".provenance.json");
}

void write_provenance(const fs::path &path, const Provenance &provenance) {
  auto native_participants = provenance.native_participants;
  std::sort(native_participants.begin(), native_participants.end(),
            [](const ProvenanceNativeParticipant &left,
               const ProvenanceNativeParticipant &right) {
              return left.name < right.name;
            });

  auto process_participants = provenance.process_participants;
  std::sort(process_participants.begin(), process_participants.end(),
            [](const ProvenanceProcessParticipant &left,
               const ProvenanceProcessParticipant &right) {
              return left.name < right.name;
            });

  auto command_files = provenance.command_files;
  std::sort(command_files.begin(), command_files.end(),
            [](const ProvenanceCommandFile &left,
               const ProvenanceCommandFile &right) {
              if (left.participant != right.participant)
                return left.participant < right.participant;
              const std::string left_path =
                  left.file ? left.file->path : std::string();
              const std::string right_path =
                  right.file ? right.file->path : std::string();
              return left_path < right_path;
            });

  json native = json::array();
  for (const ProvenanceNativeParticipant &participant : native_participants) {
    native.push_back({{"name", participant.name},
                      {"library", artifact_json(participant.library)}});
  }

  json processes = json::array();
  for (const ProvenanceProcessParticipant &participant : process_participants) {
    processes.push_back({{"name", participant.name},
                         {"command", participant.command},
                         {"executable", artifact_json(participant.executable)}});
  }

  json named_files = json::array();
  for (const ProvenanceCommandFile &named : command_files) {
    named_files.push_back({{"participant", named.participant},
                           {"file", artifact_json(named.file)}});
  }

  json errors = json::array();
  for (const ProvenanceError &error : provenance.errors) {
    errors.push_back({{"artifact", error.artifact},
                      {"path", error.path},
                      {"reason", error.reason}});
  }

  json document = {
      {"artifacts",
       {{"clock_shim", artifact_json(provenance.clock_shim)},
        {"command_files", std::move(named_files)},
        {"native_participants", std::move(native)},
        {"process_participants", std::move(processes)},
        {"runner", artifact_json(provenance.runner)}}},
      {"errors", std::move(errors)},
      {"machine_class",
       {{"architecture", provenance.machine_architecture},
        {"id", provenance.machine_class_id},
        {"libc", provenance.machine_libc},
        {"os", provenance.machine_os}}},
      {"manifest_hash", provenance.manifest_hash},
      {"recording",
       provenance.recording_sha256
           ? json{{"sha256", *provenance.recording_sha256}}
           : json(nullptr)},
      {"run_exit_code", provenance.run_exit_code},
      {"schema_version", Provenance::kSchemaVersion},
      {"sil",
       {{"source_repository", provenance.source_repository},
        {"source_revision", provenance.source_revision},
        {"version", provenance.sil_version}}},
  };

  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  if (!output)
    throw std::runtime_error("cannot open provenance side-car '" +
                             path.string() + "' for writing: " +
                             std::strerror(errno));
  output << document.dump() << '\n';
  if (!output)
    throw std::runtime_error("cannot write provenance side-car '" +
                             path.string() + "'");
}

}  // namespace sil
