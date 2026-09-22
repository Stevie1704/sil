# Support statement

What SiL supports, what it does not, and how far its guarantee reaches. Read
this before you build a process on top of SiL.

## Not safety-qualified

**SiL is not safety-qualified. It supplies no safety case, no tool
qualification evidence, and no tool confidence argument.**

It has no ISO 26262 tool classification, no tool qualification report, no
certification by any body, and no qualification plan. Nothing in this
repository may be cited as qualification evidence for a safety-related
development process.

SiL is a regression-testing tool. A test that passes under SiL says that the
Participants behaved as the test asserted, on that machine class, with those
artifacts. It says nothing about the safety of the vECU it ran, and a
determinism check is not a verification argument.

If your process needs a qualified tool, qualify it yourself under your own
process, on your own machine class, and take ownership of the result.

## Determinism: the exact boundary

**Same artifacts + same machine class ⇒ bit-identical Recordings. The
guarantee stops there. It does not extend across machine classes.**

"Same artifacts" means the identical compiled `sil-run`, the identical
Participant binaries, the identical Manifest bytes, and the identical
dependency versions. A rebuild is a new artifact.

"Same machine class" means the same OS, the same CPU architecture, and the
same libc. Two results from two machine classes may differ in their last
floating-point bit, and nothing in SiL prevents that: the framework does not
own the floating-point behavior of the code inside a Participant, and it does
not police it.

What follows:

- A Recording from another machine class is not a valid bit comparison.
  Compare it with a tolerance, the way a Reference result is compared.
- Pin the container image by digest, not by a mutable tag. The digest is the
  artifact identity a deterministic CI Run needs.
- A developer machine gets best-effort reproduction, not the guarantee.
- The determinism check (`sil-check`) proves determinism for one Manifest on
  one machine class at one moment. It does not certify a machine class.

Each Run writes a deterministic JSON provenance side-car next to its Recording:
`<recording>.provenance.json` by default, or the path selected with
`--provenance`. It records the resolved runner, Clock shim, Native libraries,
Process executables, and the files those commands name (an imported FMU archive
among them), their SHA-256 digests, the Manifest hash, the SiL version, and the
machine class. Every Manifest-named path, Process commands included, resolves
against the Manifest's directory. With `--no-recording`, the side-car is
still emitted next to the default `out.mcap` location and its `recording` value
is `null`.

## Supported machine class

| Property | Supported |
| --- | --- |
| Operating system | Linux |
| CPU architecture | x86-64 |
| libc | glibc, Debian bookworm level or newer |
| Python | 3.11 or newer, as the distribution metadata declares; the image pins 3.13 |
| Reference environment | the pinned `runtime` container image in [Dockerfile](Dockerfile) |

Continuous integration runs on x86-64 Linux with one interpreter, and that is
the only machine class this project verifies. An older supported Python is
declared, not exercised. The pinned base image also resolves an arm64
layer, so an arm64 build may succeed; it is untested, and no determinism claim
covers it. macOS and Windows are not supported; the Clock shim is a
`LD_PRELOAD` library and has no counterpart there.

## Covered by the compatibility policy

These are the interfaces a consumer is expected to depend on. A change that
breaks one of them is announced in the release notes of the release that
carries it.

| Interface | Where it is defined | Current version |
| --- | --- | --- |
| Native participant C ABI | [include/sil/participant.h](include/sil/participant.h), plus `arena.h` and `clock_region.h` | `SIL_ABI_VERSION 1` |
| Manifest document | the `sil.manifest` builder and the kernel validator | as validated by the shipped release |
| Step protocol | [docs/step-protocol.md](docs/step-protocol.md) | protocol 2, protocol 1 still accepted |
| Recording contract | MCAP, uncompressed, virtual timestamps only, Manifest hash embedded | as written by the shipped release |
| Runner command line | `sil-run`, `sil-run --version`, `sil-run --build-info`, and the exit codes `0` ok, `1` run or test failure, `2` Manifest error, `3` determinism violation | as shipped |
| Native schema-generation tool | installed `silschema` command (`bin/silschema`, shipped together with `bin/_sil_schema_types.py`) and its generated-header contract | as shipped |
| Python API | `sil.manifest`, `sil.participant`, `sil.testing`, `sil.footprint`, `sil.check`, `sil.recording`, `sil.schema`, `sil.fmi`, and the `sil-*` console entry points | package version |
| Container entry point | `sil-run` as `ENTRYPOINT`, `/workspace` as the mount point, non-root UID 10001 | image version and digest |

SiL is below 1.0. A breaking change is possible in any release, but it is named
in that release's notes and never made silently.

When relocating the installed schema generator, keep `silschema` and
`_sil_schema_types.py` together. The companion file is required distribution
content; its Python names are private implementation details, not a supported
import API. The command still requires only Python's standard library.

## Explicitly implementation details

Do not build on these. They change without notice and without a release note.

- Kernel internals: scheduler ordering structures, the interceptor plan,
  router internals, and every C++ header under `kernel/`.
- `sil-run-instrumented` and every other development or diagnostic build
  target. They are not part of a production installation.
- Anything under `participants/`, `tests/`, `tools/bench_*`, and the schemas
  that serve them. They are fixtures for this repository's own checks.
- The `acceptance` container image target and everything it adds under
  `/opt/sil/reference`.
- The container filesystem layout beyond the entry point and `/workspace`:
  installation paths, the Python virtual environment location, and layer
  structure.
- Any Python name with a leading underscore, and any module not listed in the
  table above.
- Diagnostic message text. Depend on exit codes, not on strings.
- Manifest hash values across releases. The hash identifies one Manifest for
  one release; a release may change canonicalization, and it says so when it
  does.

## What support means

SiL is maintained by one person, in the open, with no service level. Fixes go
into the latest release only; see [SECURITY.md](SECURITY.md) for the version
policy and for vulnerability reports. Questions and defects belong in
[GitHub issues](https://github.com/Stevie1704/sil/issues). There is no private
support channel and no paid support.
