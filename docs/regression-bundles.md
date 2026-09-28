# Regression bundles

A regression bundle packages one adopter regression. The target is a shared
library, one FMU, or several coupled FMUs. The bundle runs offline from the
installed SiL runtime. Each Run is attributable to exactly the artifacts
that it was prepared with. The bundle uses the preparation and verification
mechanics of the ACC acceptance bundle (`proofs/acc-fmi/acceptance-bundle.sh`)
for any target.

`sil-bundle` has three commands:

| Command | Where it runs | What it does |
|---|---|---|
| `sil-bundle seal <bundle>` | the runtime, once | Digests every bundle file, the runner and its build identity, and every declared dependency. Writes `bundle.lock.json` and prints its digest. |
| `sil-bundle verify <bundle> [--expect-lock <sha256>]` | the runtime | Re-derives every sealed identity. Exit 2 when one differs; the diagnostic names every difference. |
| `sil-bundle run <bundle> -o <evidence> [--expect-lock <sha256>]` | the runtime, offline | Verifies, then executes each declared Run into a separate evidence directory. Exit 1 when a verdict fails, 2 when the bundle is refused. |

The lock is inside the bundle. It shows an accidental change, but a person
who changes the bundle can also seal it again. Keep the lock digest that
`seal` prints apart from the bundle, and give it to `--expect-lock`. Then a
bundle that was changed and sealed again is refused.

## Prepare

Preparation is the one step that needs a source checkout, a compiler, an
exporter or an independent importer. It writes the bundle directory at the
location the runtime will use, because an authored Manifest names its
Recordings, FMU archives and libraries by absolute path. A bundle that is
moved after sealing is refused. In a container, copy the bundle to the same
path it was prepared at.

The bundle holds:

- the authored Manifests;
- the targets: a shared library, FMU archives;
- the participant code, such as a library adapter and its binding;
- the Recordings, and the conversion inputs and receipts they came from;
- the references, prepared beforehand, and the comparison contracts;
- `bundle.json`, the declaration.

`examples/bundle/prepare.py` prepares the three examples:

```sh
python examples/bundle/prepare.py library /bundles/library \
    --python-bin /opt/sil/venv/bin --sil-bin /opt/sil/bin \
    --library build/example-speed_filter.so
python examples/bundle/prepare.py fmu-replay /bundles/fmu-replay \
    --python-bin /opt/sil/venv/bin --sil-bin /opt/sil/bin \
    --fmu-binary build/EgoMotion.so
python examples/bundle/prepare.py coupling /bundles/coupling \
    --python-bin /opt/sil/venv/bin --sil-bin /opt/sil/bin \
    --fmu tests/fixtures/reference-fmus/3.0/Feedthrough.fmu --runner build/sil-run
```

| Bundle | Runs | Verdicts |
|---|---|---|
| `library` | recorded speed replayed into `speed_filter` through two adapter instances | in-run Test participant (KPI), determinism check |
| `fmu-replay` | recorded acceleration replayed into `EgoMotion` | determinism check, comparison with the closed-form reference |
| `coupling` | two coupled `Feedthrough` FMUs, and the loop with `left` replaced by its Recording | determinism checks, comparison of the retained outputs with the prepared coupled Run |

## The declaration

```json
{
  "sil_bundle": 1,
  "name": "library",
  "artifacts": {
    "library.json": "manifest",
    "speed_filter.so": "target",
    "adapter.py": "participant",
    "binding.py": "participant",
    "filter_test.py": "participant",
    "mapping.json": "conversion-input",
    "signals.csv": "conversion-input",
    "signals.mcap": "recording",
    "signals.receipt.json": "receipt"
  },
  "runtime": {
    "runner": "sil-run",
    "environment": {"PATH": "/opt/sil/venv/bin:/opt/sil/bin",
                    "PYTHONNOUSERSITE": "1"}
  },
  "dependencies": {
    "executables": ["python3"],
    "python": {"interpreter": "python3",
               "modules": ["sil", "mcap", "ctypes", "_ctypes"]},
    "files": []
  },
  "excluded": {
    "executables": ["cc", "gcc", "clang", "c++"],
    "modules": ["fmpy", "pythonfmu3"]
  },
  "runs": [
    {"name": "library", "manifest": "library.json",
     "participant_timeout_ms": 10000, "determinism": true, "comparisons": []}
  ]
}
```

- `artifacts` is the explicit artifact list. Every bundle file except the
  lock must be in it, with one of the roles `manifest`, `target`,
  `participant`, `resource`, `recording`, `conversion-input`, `receipt`,
  `reference` or `contract`.
- `runtime.environment` is the whole environment of every Run. Nothing is
  inherited from the shell that calls `sil-bundle`. `run` adds only
  `PYTHONDONTWRITEBYTECODE=1`, so an imported module does not write
  bytecode into the bundle.
- `runtime.runner` and `dependencies.executables` are resolved on the
  declared `PATH` and digested.
- `dependencies.python.modules` names the modules the participants import.
  The declared interpreter resolves each one in the declared environment.
  A package is digested over all its files, and a module over its file,
  which includes native extension modules such as `_ctypes`.
- `dependencies.files` names files outside the bundle by absolute path,
  such as a vendor library installed on the runtime, or a transitive
  native library a target loads. On Linux, `examples/bundle/prepare.py`
  declares the libraries that `ldd` resolves for the library target and
  for the FMU binary.
- `excluded` names what must not be reachable. `seal` and `run` refuse the
  bundle when one is found.
- `runs[].comparisons` names a contract and a reference, which must be
  bundle artifacts with those roles. A reference is never made during a
  Run.
- `seal` refuses a Manifest that names an existing absolute path that is
  neither in the bundle nor a declared file.

## What the seal closes, and what it does not

A Manifest hash covers the Manifest's bytes only. A Run's provenance
side-car covers the runner, the clock shim, and the files a command names.
Neither covers a Python module that an adapter imports, a native library
that a target loads, or an environment variable that a participant reads.
The declaration names those explicitly, and the lock records their
identities. Whatever the declaration does not name is not verified. The
seal does not make an incomplete declaration complete.

A runtime image pinned by its digest closes more of the environment than a
declaration. `sil-bundle` does not replace that. Run the bundle in the
pinned image, and seal it there, so that the lock records the image's
interpreter, modules and runner.

## Run and evidence

`run` writes nothing into the bundle. It re-hashes the bundle after the Runs
and fails the verdict when the bundle changed. The evidence directory must
be new or empty and outside the bundle:

```text
evidence/
  summary.json
  runs/<run>/run-1.mcap
  runs/<run>/run-1.provenance.json
  runs/<run>/run-1.log
  runs/<run>/run-2.*                      (determinism check)
  runs/<run>/<comparison>.comparison.json
```

Each Run executes from `runs/<run>/`, so its Run working directory is
created there. A Run passes when it exits 0, its two Recordings are
bit-identical (when `determinism` is true), and every comparison passes.
A Run that fails is not repeated and not compared.

`summary.json` holds the verdicts, the digests of the Manifests and the
Recordings, the digest of the lock and the runner's build identity. It
holds no artifact content and no absolute path, except in the refusal
text of a refused bundle. A consumer can keep the bundle and the evidence
private and share only the summary. The verification contract is the same
for a private bundle.

## Failure cases

| Case | Result |
|---|---|
| A bundle file altered, removed or added after sealing | `run` exit 2, no Run started, `summary.json` verdict `refused` |
| A bundle changed and sealed again | with `--expect-lock`, `run` exit 2 |
| A lock that is not a valid lock | exit 2 |
| A declared dependency missing or changed | `run` exit 2, no Run started |
| A compiler, exporter or independent importer reachable | `seal` and `run` exit 2 |
| The bundle moved from its sealed location | exit 2 |
| A Recording that leaves its reference's contract | `run` exit 1, comparison verdict `fail` |
| A target that fails its in-run KPI | `run` exit 1, Run exit code 1 |
| Two Runs that record different bytes | `run` exit 1, determinism verdict `fail` |

`tests/test_regression_bundle.py` demonstrates each case from a staged
installation. The supported acceptance platform is Linux x86-64.
