#pragma once

namespace sil {

// The Run boundary's three outcomes, as CONTEXT.md defines them. They are
// shared because the provenance record reports the Run's exit code, so the
// runner and the record cannot hold two opinions about what 2 means.
constexpr int kExitOk = 0;
// A Run failure: the Run started and then went wrong.
constexpr int kExitRunFailure = 1;
// A Manifest error: the Run was rejected before any Participant was stepped.
constexpr int kExitConfigError = 2;

}  // namespace sil
