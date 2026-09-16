#pragma once

#include <filesystem>

namespace sil {

// Absolute path to the virtual clock shim library for the running sil-run.
// Development builds place libsil_clock_shim.* beside the runner; installed
// builds use the configured conventional library directory beside bin/.
// Empty if the runner's own path cannot be determined. The load-time check and
// the spawn-time injection resolve the shim through this one place so they can
// never disagree about which file a shimmed Run needs.
std::filesystem::path clock_shim_library_path();

}  // namespace sil
