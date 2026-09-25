# Issue #157: bounded CAN FMU resources and lifecycle isolation

These files are retained from `models/can/run.sh` on Linux x86-64 in the CAN
qualification image. `qualification.json` binds the FMU, Manifests, logs,
native results and the runner by SHA-256; the image ID is in
`build/can/image.txt` when reproduced. SOURCE_AND_RESULT_SUMMARY

What each file shows:

- `abi.json`: outcome counts of the bounded malformed-input corpus. Each case
  goes through the exported C entry points: accepted, answered with Format
  Error, or rejected with fmi3Error, then recovered by `fmi3Reset`. The same
  program (`tests/abi.cpp`) checks repeated lifecycles, initialization failure,
  reset from every state, free with traffic in flight, interleaved calls of
  differently configured instances (one of them in Error state), Binary
  output ownership, the logging callback, and invalid arguments.
- `capacity.json`: the declared per-instance limits and, separately, the C++
  heap bytes one instance requested when filled to those limits
  (`tests/capacity.cpp`). The measurement excludes allocator overhead, belongs
  to this build and toolchain, and is no OS memory isolation.
- `issue157-sil.json` and `issue157-*.json`/`*.log`: SiL Runs through the
  existing FMU group. Corrupt operations reach the Recording as Format Error to
  the sending terminal one nanosecond later, and a repeated Run yields
  identical Recording bytes. An unsupported extended frame and an exhausted
  queue each end the Run with exit code 1, the FMU's diagnostic, and no Run
  working directory or FMU extraction left in the invocation directory.
- `sanitizers.txt`: which sanitizers the native checks ran with.

The profile behaviour and the response to each malformed case are specified in
[`models/can/README.md`](../../README.md#malformed-traffic-and-resource-bounds).
The earlier evidence directories and the upstream proof under
`proofs/fmi-ls-bus/` are unchanged.
