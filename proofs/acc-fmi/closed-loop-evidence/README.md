# Native closed-loop evidence

Captured from [Actions run 35711429115](https://github.com/Stevie1704/sil/actions/runs/35711429115),
`closed-loop` job, before this evidence-only commit. The exact tested source
revision, CPython/runtime packages, runner and image identities, and machine
class (Linux x86-64, glibc 2.36) are retained in `environment.json` and
`image-id.txt`. Both FMU archive identities and bundled source identities are
in `archives.json` and the two `*.identity.json` files. The complete archives,
raw traces, both repeat Recordings, schema audit and logs are in that run's
`acc-loop-evidence` artifact. This directory does not replace #147's baseline.

The 500 nominal communication points match FMPy numerically. The minimum gap,
including the final state at 5 s, is **49.96364767075116 m**, above the 5 m KPI.
The unchanged Python example also matches its separately declared causal
schedule. Both Runs of each Manifest are byte-identical. All three coupling
negative controls fail at the signal/instant reported in `results.json`;
unknown and wrong-causality bindings fail with exit 2. Fourteen gate tests pass.

Retention:

- Exact Manifests and one Recording with provenance per successful Manifest.
  Both repeat hashes remain in `results.json`; the duplicate bytes stay in CI.
- `configuration.json`: periods, Latency, finite capacity, fields, units, KPI
  and predeclared per-signal tolerances.
- `initialization.json`: FMPy initialization outputs for both nominal and
  original-schedule execution.
- `nominal.fmpy.csv` and `original.fmpy.csv`: lossless numeric projection of the
  independent driver's JSON, including interval boundaries, sensing, positions,
  command, sampled sensing and applied acceleration. `interval_end_ns` means
  the end of doStep, not necessarily the time of every output: nominal sensing
  describes that end; original sensing describes `publication_ns`. Original
  command zero at publication zero means the initial held plant input, not a
  controller Message (the comparator correctly requires no Message there).

To regenerate, run the command in the parent README and retain a new identified
result. Do not treat regenerated archive or Recording bytes as this snapshot.
