# SiL Framework

Deterministic software-in-the-loop testing for ADAS/AD: a C++20 kernel
schedules Participants in Virtual time and routes typed Messages. The same
artifacts on the same machine class produce bit-identical Recordings.

Native Participants use a stable C ABI. Process Participants use a JSON-lines
Step protocol, with Python support for authoring Manifests, importing FMUs,
replaying recorded input, and checking results. FMI and CAN behavior stays at
the edge, outside the kernel.

Read [SUPPORT.md](SUPPORT.md) for the supported machine class, compatibility
policy, and scope of the determinism guarantee. SiL is not safety-qualified.

## Quick start

From a checkout, with CMake 3.24+, a C++20 toolchain, Python 3.11+, and `uv`:

```sh
make venv
make build
make example
make test
```

`make example` runs the packaged ACC reference example and writes its Manifest
and Recording to `build/acc.json` and `build/acc.mcap`. `make test` runs the
C++ and Python suites, including checks that repeated Runs produce identical
Recording bytes. Use `make help` to list the other commands.

Run your own Manifest and check its determinism:

```sh
make run ARGS="manifest.json -o build/run.mcap"
make check ARGS="manifest.json"
```

An installed SiL has one command for its tools: `sil --help` lists them
(`sil check`, `sil compare`, `sil run`, `sil fmi inspect`, …).

See [examples and installation](docs/getting-started.md) for the ACC example
and staged installation, or [container deployment](docs/container.md) to run
one Manifest in a Linux container. Detailed [Run behavior and guards](docs/running.md)
cover exit codes, response deadlines, the Clock shim, and Transport.

## Architecture and layout

```text
Python authoring and test tools → Manifest → C++ kernel → Recording
                                              ↕
                         Native / Process / Replay Participants
                                              ↕
                            FMI importer and external models
```

The kernel owns scheduling, routing, and recording. Participants supply the
behavior being simulated; the Importer owns FMI-specific coordination, and
maintained models own their domain semantics.

| Location | Responsibility |
| --- | --- |
| `kernel/`, `include/sil/`, `shim/` | Runtime, public C interfaces, and the Clock shim |
| `python/src/sil/` | Authoring, Participant support, FMI import, replay, and verification |
| `models/` | Maintained model products with their own qualification and releases |
| `examples/` | Runnable guides for adopters; the packaged ACC example is in `python/src/sil/examples/` |
| `tests/` | Behavior tests, native tests, and fixtures under `tests/fixtures/` |
| `proofs/` | Acceptance experiments with pinned third-party artifacts and retained evidence |
| `tools/`, `cmake/`, `container/`, `.github/` | Developer tools, packaging, and CI |
| `worker/`, `factory.yaml` | Development-agent environment and automation configuration |
| `docs/` | User guides, protocols, architecture decisions, and benchmark reports |

See the [architecture guide](docs/architecture.md) for the interfaces, build
ownership, and where to put changes.

## Documentation

The [documentation index](docs/README.md) links every topic guide and explains
which documents define terminology, behavior, and architectural decisions.

- [FMI importer](docs/fmi.md) and [coupling or substituting FMUs](docs/fmu-coupling.md)
- [Recorded input and FMU replay](docs/recorded-input.md)
- [Shared-library participants](docs/library.md)
- [Replay windows and long Recordings](docs/replay.md)
- [Trajectory comparisons](docs/comparison.md) and [regression bundles](docs/regression-bundles.md)
- [Acceptance proofs and maintained models](docs/acceptance.md)
- [Routing performance](docs/performance.md)

## License, security, and support

- [LICENSE](LICENSE) — Apache License 2.0, SPDX `Apache-2.0`, covering the
  kernel, the public C ABI headers, the Clock shim, the Python distribution,
  and the schema-generation surface a Participant compiles against.
- [NOTICE](NOTICE) — the attribution notice the license propagates.
- [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) — every third-party
  component in a published artifact, with its license.
- [SECURITY.md](SECURITY.md) — how to report a vulnerability privately, what a
  report should contain, what response to expect, and which versions get fixes.
- [SUPPORT.md](SUPPORT.md) — supported machine class, the interfaces under the
  compatibility policy, the interfaces that are implementation details, the
  boundary of the determinism guarantee, and the absence of any safety
  qualification.
- [CONTRIBUTING.md](CONTRIBUTING.md) — the terms a contribution is accepted
  under, and how to get a change reviewed.
- [docs/releasing.md](docs/releasing.md) — how maintainers prepare, publish,
  verify, and recover a versioned release bundle.
