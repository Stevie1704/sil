#include "clock_shim.hpp"

#ifdef __APPLE__
#include <mach-o/dyld.h>

#include <cstdint>
#include <string>
#else
#include <unistd.h>
#endif

namespace sil {

namespace {

#ifndef SIL_INSTALL_BINDIR
#define SIL_INSTALL_BINDIR "bin"
#endif

#ifndef SIL_INSTALL_LIBDIR
#define SIL_INSTALL_LIBDIR "lib"
#endif

#ifdef __APPLE__
constexpr const char *kClockShimLib = "libsil_clock_shim.dylib";
#else
constexpr const char *kClockShimLib = "libsil_clock_shim.so";
#endif

// Directory holding the running sil-run binary.
std::filesystem::path runner_dir() {
#ifdef __APPLE__
  uint32_t size = 0;
  _NSGetExecutablePath(nullptr, &size);
  std::string buf(size, '\0');
  if (_NSGetExecutablePath(buf.data(), &size) != 0) return {};
  return std::filesystem::weakly_canonical(buf.c_str()).parent_path();
#else
  return std::filesystem::weakly_canonical("/proc/self/exe").parent_path();
#endif
}

std::filesystem::path installed_prefix(
    const std::filesystem::path &runner_directory) {
  const std::filesystem::path bindir = SIL_INSTALL_BINDIR;
  if (bindir.is_absolute()) return {};

  std::filesystem::path prefix = runner_directory;
  for (const auto &component : bindir) {
    if (component == "..") return {};
    // GNUInstallDirs preserves a trailing slash in values such as
    // "custom/bin/".  std::filesystem iterates that as an empty component;
    // it is not another directory to strip from the installed prefix.
    if (!component.empty() && component != ".") {
      prefix = prefix.parent_path();
    }
  }
  return prefix;
}

}  // namespace

std::filesystem::path clock_shim_library_path() {
  std::filesystem::path dir = runner_dir();
  if (dir.empty()) return {};
  // The build tree keeps the shim beside the runner for the existing
  // development workflow.  An installed runner uses the conventional
  // <prefix>/bin and <prefix>/<libdir> layout, so resolve that sibling only
  // when the build-tree location is absent.
  const std::filesystem::path beside_runner = dir / kClockShimLib;
  std::error_code error;
  if (std::filesystem::exists(beside_runner, error)) return beside_runner;
  const std::filesystem::path libdir = SIL_INSTALL_LIBDIR;
  if (libdir.is_absolute()) return libdir / kClockShimLib;
  const std::filesystem::path prefix = installed_prefix(dir);
  if (prefix.empty()) return {};
  return prefix / libdir / kClockShimLib;
}

}  // namespace sil
