# Examples and installation

[Documentation index](README.md) · [Repository overview](../README.md)

## Install a release

A release has two parts: the wheel (the `sil` command, the Python package and
the docs) and the native development archive (`sil-run`,
`sil-run-diagnostics`, `silschema`, the clock shim and `include/sil`). Both
are assets of the GitHub release. The runtime image is another way to run
SiL, with nothing to install but Docker (`sil docs container`).

On Linux x86-64, the install script of a release installs both parts. It
needs `curl`, `tar` and Python 3.11 or newer with `venv` (on Debian and
Ubuntu, the `python3-venv` package):

```sh
curl -fsSL https://github.com/Stevie1704/sil/releases/download/v<VERSION>/install.sh | sh
. ~/.local/share/sil/env
```

- It checks each download against the release's `SHA256SUMS` before it
  installs anything, and installs nothing when one does not match.
- It installs into `~/.local/share/sil/<VERSION>/` (`--prefix DIR` for
  another directory): a virtual environment, `venv/`, and the native archive,
  `native/`. Versions stay side by side, and an installed version is never
  changed. `current` points at the version installed last.
- `~/.local/share/sil/env` puts `current/venv/bin` and `current/native/bin`
  first on `PATH`. Source it in your shell profile. Then `python3` is the
  Python of the virtual environment, which Python Participants need
  (`python3 -m sil.participant`).
- `--version VERSION` installs another release. Run `sh install.sh --help`
  for all options. A release before 0.2.1 has no `install.sh` asset: use the
  script of a later release, or of `tools/install.sh` in the repository, with
  `--version`.
- From 0.2.1, the native archive runs on Debian bookworm (glibc 2.36) or
  newer. The published 0.1.0 and 0.2.0 archives need glibc 2.38 and
  `GLIBCXX_3.4.32`, for example Ubuntu 24.04. Use their runtime image on
  bookworm; those published archives are not replaced.
- On Windows and macOS, the script stops before any download. See
  [Windows and macOS](#windows-and-macos).

Without the script, download the wheel, the native archive and `SHA256SUMS`
from the release, check them with `sha256sum -c --ignore-missing SHA256SUMS`,
install the wheel into a virtual environment, unpack the archive, and put
both `bin` directories on `PATH`.

## Windows and macOS

SiL releases support Linux x86-64 only (`sil docs support`). On Windows and on
macOS, run that Linux x86-64 build in WSL2 or in Docker.

**Windows: WSL2.** WSL2 runs a real Linux x86-64 kernel, so this is the
supported machine class on an x86-64 PC. In PowerShell, install Ubuntu 24.04,
then use the install script in its shell:

```sh
wsl --install -d Ubuntu-24.04
# In the Ubuntu shell:
sudo apt-get install -y curl python3-venv
curl -fsSL https://github.com/Stevie1704/sil/releases/download/v<VERSION>/install.sh | sh
. ~/.local/share/sil/env
```

Keep the Manifests, FMUs and Recordings in the Linux file system, for example
under `~`, not under `/mnt/c`: the Windows drives are slower and do not keep
Linux file modes.

**Windows and macOS: Docker.** The runtime image is the reference environment.
Pull it by the digest in the release notes and run it as
`sil docs container` describes. The image is for `linux/amd64` only; on a Mac
with Apple silicon, give `--platform linux/amd64` to each `docker pull` and
`docker run`, and Docker Desktop emulates it:

```sh
image=ghcr.io/stevie1704/sil@sha256:<release-image-digest>
docker pull --platform linux/amd64 "$image"
docker run --rm --platform linux/amd64 --network none \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD,dst=/workspace" \
  "$image" /workspace/manifest.json -o /workspace/out.mcap
```

- The emulated Run is the same machine class as the reference, but it is
  slower. Do not use its diagnostics or benchmark times as the speed of the
  SUT.
- Docker Desktop on macOS shares only some directories with containers, by
  default your home directory. Keep the workspace under it.
- On Windows, run the `docker` commands in a WSL2 shell, where `$PWD` and
  `id` exist, or give the paths and the user in PowerShell form.
- The image entry point is `sil-run`. For the `sil` command, give
  `--entrypoint sil`, for example `--entrypoint sil "$image" docs`.

**Not supported:** a native Windows build, a native macOS build, and Linux
arm64. A build from source on macOS can serve for development, but it is not
a release, no determinism claim covers it, and the clock shim has no macOS
counterpart.

## Use the installed framework

From any working directory, without the SiL source checkout:

```sh
sil docs agent-guide
sil skill install
python -m sil.examples.acc.manifest acc.json
sil run acc.json -o acc.mcap
sil check acc.json
sil examples --dir examples
```

`sil docs examples` describes the exported adapters, bindings, recorded
inputs and bundle preparation scripts. The following build instructions are
for contributors preparing those release artifacts from a checkout.

## ACC reference example

[python/src/sil/examples/acc/](../python/src/sil/examples/acc/) is one closed loop
end to end: a plant carrying
the longitudinal motion of two vehicles, a shimmed vECU controller, and a test
participant holding the Run to a minimum-gap KPI. The packaged
`sil.examples.acc.manifest` module is the whole Run — three participants, two
Channels, and the Duration — and the pytest suite in `tests/test_example_acc.py`
imports that same module rather than restating it.

```sh
make example                                    # run it, record it into build/
```

The Manifest starts each Participant with `python3`, and `sil-run` finds
`python3` on its own `PATH`. Activate the environment that has `sil` installed
before you start the Run. If you do not, the Run fails with `No module named
'sil'`. The command does not use an absolute path, so the source Manifest and
the wheel Manifest have the same hash.
[Process participant command](running.md#process-participant-command) gives
the rule for Manifests that you write.

It ships in two variants. The second declares one Interceptor — five Steps of
delay on the sensing Channel over a one-second window — and comes from the same
builder:

```sh
PYTHONPATH=$PWD/python/src .venv/bin/python -m sil.examples.acc.manifest \
    build/acc.json
PYTHONPATH=$PWD/python/src .venv/bin/python -m sil.examples.acc.manifest \
    --delayed-sensing build/acc-delayed.json
```

The two hashes differ, which says the Runs are not the same Run;
`tests/test_example_acc.py` is what says the Interceptor is the whole of the
difference, by taking it back out of the delayed Manifest and getting the
nominal one. Late sensing is late braking: the delayed Run drives a measurably
different trajectory, which is what shows the Interceptor doing something
rather than merely being declared. Both variants are in the CI determinism
gate.

## Install and run from a staged prefix

The production runner, virtual Clock shim, and public Native participant
headers can be staged independently of the build tree. The Python wheel
contains the `sil` package, the ACC reference Run, the `sil` command, and the
user docs that `sil docs` prints. Build the two parts from the repository.
`stamp-python` copies the Python tree and puts the docs and the release
metadata into it, as the release build does:

```sh
python3 tools/release.py stamp-python --output-dir build/python-release \
    --version "$(python3 tools/release.py project-version)" \
    --source-revision "$(git rev-parse HEAD)"
uv build --wheel --out-dir dist build/python-release
wheel=$(find dist -maxdepth 1 -name 'sil-*.whl' -print -quit)
uv venv .venv-staged
uv pip install -p .venv-staged/bin/python "$wheel"

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
prefix=$(mktemp -d)
cmake --install build --prefix "$prefix"
```

Add both installation `bin` directories to `PATH`, then run from any directory
outside the checkout. No `PYTHONPATH`, build-tree path, or preload-library path
is needed:

```sh
export PATH="$PWD/.venv-staged/bin:$prefix/bin:$PATH"
workdir=$(mktemp -d)
cd "$workdir"
python -m sil.examples.acc.manifest acc.json
sil-run acc.json -o acc.mcap
sil check acc.json --runner sil-run

python -m sil.examples.acc.manifest --delayed-sensing acc-delayed.json
sil-run acc-delayed.json -o acc-delayed.mcap
sil check acc-delayed.json --runner sil-run
```

The installed runner resolves the Clock shim from the staged prefix's
conventional library directory. The installed headers are under
`$prefix/include/sil`; development fixtures and `sil-run-instrumented` are
not part of the production installation.
