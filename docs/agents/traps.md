# Known traps

Agent sessions on this repo find these environment traps many times. They
are facts for contributors, not user docs. Each entry gives the symptom, then
the fix. Remove an entry when it is no longer true.

## Docker and proofs

- **Proof workspace under `/var/folders` or `/tmp`.** Symptom: a
  `proofs/*/run-proof.sh` script fails with `invalid mount config for type
  "bind": bind source path does not exist`, for a path that exists. Cause: the
  default workspace is `mktemp -d`, and Docker Desktop on macOS shares only
  `/Users`. Fix: give the workspace as the second argument, under `$HOME`, for
  example `./proofs/fmi-ls-bus/run-proof.sh "$PWD/proofs/fmi-ls-bus/evidence"
  "$HOME/.cache/sil-lsbus-ws"`. Delete the workspace after the run.
- **ACC FMI proofs on arm64.** Symptom: a local `proofs/acc-fmi/**` run or
  `acceptance-bundle.sh prepare` builds for minutes, then fails on the
  machine-class check. Cause: the PythonFMU3 archives contain only x86-64
  binaries, and the proofs require `x86_64` Linux. The legacy Docker builder
  ignores `--platform linux/amd64` for the multi-platform base digest, so the
  proof image builds as arm64. Fix: on an arm64 Mac, run
  only what needs no FMU binary (the gate tests, `ruff`, the `tests/` suite).
  CI on x86-64 is the real gate.
- **amd64 runtime image without buildx.** Symptom: the legacy builder says the
  base image `does not provide the specified platform`, or it builds arm64
  with no error. Fix: pass `--build-arg PYTHON_IMAGE=python@sha256:...` with
  the amd64 manifest of the pinned base index. The comment in
  `models/can/bundle.sh` gives the digest, and that script reads it from
  `CAN_RUNTIME_PYTHON_IMAGE`. Under qemu, `cc1plus` sometimes segfaults while
  it compiles the kernel. That is a flake: run the build again.
- **CMake sources outside the Dockerfile build stage.** Symptom: pytest passes
  locally, but the `test`, `Build and smoke-test release shapes` and `qualify`
  CI jobs fail at CMake generate. Cause: the root `Dockerfile` build stage
  copies only `include`, `kernel`, `shim`, `tests`, `tools`, `cmake`,
  `python`, `container` and `docs`. Fix: guard a target whose sources are
  elsewhere (for example `examples/`) with `if(EXISTS ...)`, or copy its
  directory in the Dockerfile.
- **Docker proofs copy the sources at start.** Symptom: a source edit made
  while a proof runs is not in its result. The committed `image-id.txt`
  changes with any source change. Fix: finish and commit the edits, then start
  the proof.

## Native builds

- **macOS 27.0 SDK.** Symptom: linking a shared library fails with `ld: tapi
  error: malformed file ... libSystem.tbd: unknown architecture
  arm64e.x1-macos`. Targets already in `build/` hide it until something
  relinks. Fix: `export
  SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk` before
  builds and before tests that compile (the staged-install tests call `cc`).
  Do not change the repo to work around it.

## Flaky tests

Run a flaky test again alone before you investigate. If it passes and the
branch does not touch the code that it names, report it as intermittent.

- **Library example `TestIncorrectOutput`.** Symptom:
  `tests/test_example_library.py::TestIncorrectOutput` fails with
  `participant 'filter_test' exited unexpectedly`. Tracked in #288.
- **`setpgid` at Process participant spawn.** Symptom: under load, a test that
  starts Process participants fails with `sil: setpgid participant: Operation
  not permitted`. Seen in `test_fmu_substitution`, `test_adas_closed_loop`,
  `test_fmi_group`, `test_staged_install` and
  `test_adas_process_adapter`. Do not run two full pytest
  suites at the same time. Tracked in #289.

## Measurement and expected values

- **Linux `maxrss` survives `exec`.** Symptom: on Linux, the `wait4` peak RSS
  of a Run started from Python is at least the driver's (about 40 MiB). macOS
  does not show this. Fix: start the Run from a small C launcher, as
  `proofs/adas-cost/run_measured.c` does.
- **FMU step time is a double sum.** Symptom: expected FMU timestamps differ
  by 1 ns at some Steps (for example 0.18 + 0.02 gives 199999999 ns). Cause:
  the Importer calls `fmi2DoStep(t/1e9, dt/1e9)`. Fix: compute an expected
  time as `t/1e9 + dt/1e9`, not `(t+dt)/1e9` or `k*step`.
