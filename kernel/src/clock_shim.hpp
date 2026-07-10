#pragma once

#include <filesystem>

namespace sil {

// Absolute path to the virtual clock shim library shipped next to the running
// sil-run binary (CMake places libsil_clock_shim.* in the runner's directory).
// Empty if the runner's own path cannot be determined. The load-time check and
// the spawn-time injection resolve the shim through this one place so they can
// never disagree about which file a shimmed run needs.
std::filesystem::path clock_shim_library_path();

}  // namespace sil
