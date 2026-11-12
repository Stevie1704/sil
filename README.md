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

## Install the latest release

Use the [latest release](https://github.com/Stevie1704/sil/releases/latest)
without cloning or building this repository.

### With a coding agent

Copy this prompt into your agent in the project where you want to use SiL:

```text
Install the latest stable release of SiL (https://github.com/Stevie1704/sil)
for this project. Read the installation guide at
https://github.com/Stevie1704/sil/blob/main/docs/getting-started.md first.

Check the OS and CPU architecture. On Linux x86-64 with glibc 2.36 or newer,
ensure curl, tar, and Python 3.11+ with venv are available, then use
https://github.com/Stevie1704/sil/releases/latest/download/install.sh
to install both the Python tools and native runtime. On Windows, use WSL2;
on macOS, use the Linux amd64 Docker image pinned by digest in the latest
release notes, following the guide's platform instructions.

For a native installation, source ~/.local/share/sil/env and add that line
to my shell profile if missing. Verify sil --help and sil-run --version,
then run sil skill install in this project and read sil docs agent-guide.
Verify the installation by generating the packaged ACC example with
python -m sil.examples.acc.manifest acc.json and running sil check acc.json
in a temporary directory (use the container equivalents for Docker).
Report the installed version, verification result, and how to use SiL in
a new terminal.
```

### On your own

On **Linux x86-64 with glibc 2.36+** (Debian 12 / Ubuntu 24.04 or newer),
you need `curl`, `tar`, Python 3.11+ with `venv`, and `sha256sum` or `shasum`.
On Debian or Ubuntu, install the prerequisites with:

```sh
sudo apt-get update
sudo apt-get install -y curl tar python3 python3-venv coreutils
```

Install the latest release and put it on `PATH` in your current shell:

```sh
curl -fsSL https://github.com/Stevie1704/sil/releases/latest/download/install.sh | sh
. "$HOME/.local/share/sil/env"
sil --help
sil-run --version
```

The installer checks the wheel and native archive against the release's
`SHA256SUMS` and installs both under `~/.local/share/sil/<VERSION>/`.
The released native runtime includes OpenSSL's hashing library through static
linking; no separate OpenSSL installation is needed.
Add `. "$HOME/.local/share/sil/env"` to your shell profile (for example,
`~/.bashrc` for Bash or `~/.zshrc` for Zsh) to use SiL in new terminals.
Run the same installation command to upgrade; previous versions stay installed.

Check the packaged example in a temporary directory:

```sh
(
  cd "$(mktemp -d)" || exit 1
  python -m sil.examples.acc.manifest acc.json
  sil check acc.json
)
```

In your project directory, run `sil skill install` to give your coding agent
the installed version's guide and commands. Use `sil docs agent-guide` to
read the guide yourself.

On **Windows**, use WSL2 with Ubuntu 24.04 and run the Linux commands above
in its shell. On **macOS**, use the Linux amd64 Docker image from the release.
See [Windows and macOS](docs/getting-started.md#windows-and-macos) for setup
and container commands, or [installation details](docs/getting-started.md#install-a-release)
for a custom prefix or a specific release.

## Build from source

From a checkout, with CMake 3.24+, a C++20 toolchain, Python 3.11+, `uv`,
and OpenSSL 3.0+ headers and its static library (`libssl-dev` on Debian/Ubuntu):

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
`sil docs agent-guide` explains onboarding, and `sil examples --dir examples`
provides the adapters, bindings and preparation inputs without a checkout.
`sil skill install` gives a coding agent the guide and commands of that version.

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
