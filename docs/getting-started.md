# Examples and installation

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

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
contains the `sil` package, the ACC reference Run, and its command-line entry
points. Build the two parts from the repository:

```sh
uv build --wheel --out-dir dist python
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
sil-acc acc.json
sil-run acc.json -o acc.mcap
sil-check acc.json --runner sil-run

sil-acc --delayed-sensing acc-delayed.json
sil-run acc-delayed.json -o acc-delayed.mcap
sil-check acc-delayed.json --runner sil-run
```

The installed runner resolves the Clock shim from the staged prefix's
conventional library directory. The installed headers are under
`$prefix/include/sil`; development fixtures and `sil-run-instrumented` are
not part of the production installation.
