# Issue #158: SilCanBus 1.0.0 release evidence

These files come from `models/can/run.sh` and then `models/can/bundle.sh`, run
from clean source revision `e4f5d05181a327e99d0255015a6f8ab07d9f94fc`
(`source_dirty: false`). The release is `SilCanBus-1.0.0-x86_64-linux.fmu`,
SHA-256 `474828e086e47af5b14316e99b421283f7a8045fd33993bbfa6ffdb49317f427`.
The archive itself stays in `build/can/release/` and in the CI
`can-model-release` artifact; it is not retained here.

The run was on an ARM development host under qemu. The qualification image was
`sha256:2516c2097f3501cc350f5b73740a2913d4070af8fec9c6245f7b166d70064c00`
(Python 3.13.7, FMPy 0.3.32, GNU C++ 12.2.0). ASan cannot start under qemu, so
the native checks ran with UBSan only (`sanitizers.txt`). CI runs ASan and
UBSan natively. All 132 CAN qualification pytest cases passed.

The legacy Docker builder of that host cannot select a platform from the
pinned multi-platform base index. So the runtime image was built with
`CAN_RUNTIME_PYTHON_IMAGE` set to that index's linux/amd64 manifest,
`python@sha256:781449467ffb6f04218f09b1ecdcdc7d22b289ee5da9ec498b024e24ad7a6db7`.
The runtime image was otherwise the unchanged production `runtime` target,
`sha256:62afd2733a816f0ac8b8efc28fe34ce231631b5b48819189117440197328b67c`.

What each file shows:

- `release.json`, `SHA256SUMS`: the published digest and the compiler, flags,
  FMI header source, qualification image, Git revision and source digests.
  Two controlled builds were byte-identical before anything was staged.
- `qualification.json`: SHA-256 of every artifact, input and the runner, and
  the list of checks, including release staging, the rebuild from `sources/`
  without Python, and the example.
- `example/`: the shipped example through the independent FMPy master and
  through SiL from the source tree. Both traces equal the hand-derived table
  in `tests/test_example.py`. The SiL Run was repeated with identical
  Recording bytes.
- `bundle/`: the same example from the installed SiL bundle
  (`sil-trace.json`, `manifest.json`, provenance side-car). The imported
  `sil` module is a file of the installed `sil` distribution
  (`sil_installed`), and the Run had no network and no source tree. The independent FMPy
  path (`independent.json`) ran where `sil` could not be imported.
  The example selects a 256-byte terminal buffer capacity.
  `evidence.json` is the verdict: 9 equal trace rows, the same archive on both
  paths, repeat-identical Recordings, the deadlines, and the reference-tool
  versions.

The earlier evidence directories and the upstream beta proof under
`proofs/fmi-ls-bus/` are unchanged. This directory adds new artifacts only.
