# Native closed-loop evidence

[Native run 35735825416](https://github.com/Stevie1704/sil/actions/runs/35735825416)
passed both proofs. `report.json` retains the tested source and runtime,
machine class, both FMU identities, configuration, verdicts, determinism hashes,
and raw-artifact digests. `image-id.txt` identifies the execution image.

All four FMU variants matched independent execution at 500 communication points;
the unchanged Python example matched its separate schedule. All five Manifests
and their repeated Recordings were byte-identical. Minimum gap was
**49.96364767075116 m** (threshold 5 m), with 499 Messages checked in-run and
500 post-hoc. SiL perturbations proved both causal directions. Overflow, KPI
and binding rejection gates passed, as did all 28 focused tests.

The full FMUs, Manifests, Recordings/provenance, initialization and trajectory
JSON, schema audits and logs are in that run's **acc-loop-evidence** artifact.
They are deliberately not duplicated in Git. The compact report survives CI
artifact expiration; full inspection then requires rerunning the proof.

Regenerate this report from the downloaded artifact:

```sh
python proofs/acc-fmi/loop_evidence.py /path/to/acc-loop-evidence /path/to/report
```

The parent README's clean Docker command runs the proof and emits the report
automatically. Rebuilding creates new execution identities; this snapshot
continues to identify the native run above. Earlier development CSVs and
Recordings remain in Git history. The #147 baseline is untouched.
