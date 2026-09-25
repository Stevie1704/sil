# Issue #158: SilCanBus 1.0.0 release evidence

These files come from `models/can/run.sh` and then `models/can/bundle.sh`, run
from clean source revision `ebc1c67a311958828cc8dc1e3b44fc235aeaba6e`
(`source_dirty: false`). The release is `SilCanBus-1.0.0-x86_64-linux.fmu`,
SHA-256 `dc019747485dbb875c5b2c070b2e7caad5129ee0620290ff8a9a2e573682f13c`.
The archive itself stays in `build/can/release/` and in the CI
`can-model-release` artifact; it is not retained here.

The run was on an ARM development host under qemu. The qualification image was
`sha256:5132ce39935541dd7449f8106045aa355a9d37133d904bf08ef60eac4f450742`
(Python 3.13.7, FMPy 0.3.32, GNU C++ 12.2.0). ASan cannot start under qemu, so
the native checks ran with UBSan only (`sanitizers.txt`). CI runs ASan and
UBSan natively. All 126 CAN qualification pytest cases passed.

The legacy Docker builder of that host cannot select a platform from the
pinned multi-platform base index. So the runtime image was built with
`CAN_RUNTIME_PYTHON_IMAGE` set to that index's linux/amd64 manifest,
`python@sha256:781449467ffb6f04218f09b1ecdcdc7d22b289ee5da9ec498b024e24ad7a6db7`.
The runtime image was otherwise the unchanged production `runtime` target,
`sha256:c4f484538ffcf5b195510385879c66c758c37ab155c1d47292ad1f9eea57512a`.

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
  (`sil-trace.json`, `manifest.json`, provenance side-car). SiL came from
  `/opt/sil/python`, with no network and no source tree. The independent FMPy
  path (`independent.json`) ran where `sil` could not be imported.
  `evidence.json` is the verdict: 9 equal trace rows, the same archive on both
  paths, repeat-identical Recordings, the deadlines, and the reference-tool
  versions.

The earlier evidence directories and the upstream beta proof under
`proofs/fmi-ls-bus/` are unchanged. This directory adds new artifacts only.
