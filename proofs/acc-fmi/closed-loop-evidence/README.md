# Native closed-loop evidence

Captured from [Actions run 35735825416](https://github.com/Stevie1704/sil/actions/runs/35735825416),
which built one image and executed both the unchanged open-loop qualification
and expanded closed-loop proof. `environment.json` identifies the tested source,
CPython/runtime packages, runner and machine class (Linux x86-64, glibc 2.36);
`image-id.txt` identifies the image. Both FMU archive and bundled-source
identities are retained in `archives.json` and the two `*.identity.json` files.
The complete archives, both repeat Recordings, raw independent traces, failed
Run artifacts, schema audit and logs remain in that run's `acc-loop-evidence`
artifact. This replaces the PR's earlier development snapshot; #147's baseline
under `evidence/` is untouched.

Native verdicts:

- All four FMU coupling variants match their independent FMPy executions at
  500 communication points each. The unchanged Python example matches its own
  causal schedule, with no command Message at publication zero.
- Every one of the five successful Manifests is independently constructed
  twice with equal bytes; two Runs of each also produce equal Recording bytes.
- Nominal minimum gap is **49.96364767075116 m**, above the 5 m threshold.
  The in-run KPI checks 499 Messages, through publication 4.98 s. Post-hoc
  evaluation checks all 500, including publication 4.99 s / plant time 5 s.
- SiL command delay changes motion at publication 0.01 s. SiL sensing delay
  changes commands at 1.56 s, after the initial saturated region; no immediate
  sensitivity while saturated is claimed. The altered initial command also
  fails nominal comparison. Full first-mismatch diagnostics are retained.
- Unknown and wrong-causality bindings fail with exit 2. Insufficient route
  capacity and an unmeetable in-run KPI fail with exit 1.
- All 28 closed-loop gate tests pass.

## Regeneration and retention

Every file here except this explanatory README is emitted by the committed
producer. From a downloaded full artifact:

```sh
python proofs/acc-fmi/loop_evidence.py /path/to/acc-loop-evidence /path/to/curated
```

The parent README's clean Docker command also produces `curated/` automatically.
The host script adds the image identity after copying the container output.
Regenerating twice from the same raw artifact produces identical file bytes;
the retained projection was checked against the one generated inside CI.

Exact Manifests, one Recording with provenance per successful variant, both
repeat hashes, configuration and initialization outputs are retained here.
Duplicate Recordings and full raw JSON remain in CI. The five `*.fmpy.csv`
files are lossless numeric projections, with publication and interval-end
instants, sensing, positions, published command, actual FMU controller output,
sampled sensing and applied acceleration. `initialization.json` retains and
identifies all five independent initialization sets.

For the original schedule, sensing describes `publication_ns`; for the nominal
FMU schedule it describes `interval_end_ns`. The empty published-command cell
at zero means **no publication**, while `controller_output_mps2` retains the
real 1.5 m/s² FMU output. Neither that output nor a timestamp is changed to
make the comparison pass.

Rebuilding creates new archive and runner identities. Treat each new execution
as a new identified snapshot, not a byte replacement for this one.
