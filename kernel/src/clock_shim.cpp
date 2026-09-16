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
  return dir.parent_path() / SIL_INSTALL_LIBDIR / kClockShimLib;
}

}  // namespace sil
