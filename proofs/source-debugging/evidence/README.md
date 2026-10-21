# Retained Linux source-debugging evidence

Source: [successful Actions run 39554858402](https://github.com/Stevie1704/sil/actions/runs/39554858402).
`qualification.json` identifies its PR head, full artifact and published
SHA-256/expiry. `report.json` identifies the actual checkout merge revision,
host, pinned tools, 43 experiments and the grouped-ADAS rejection. Later
commits retain evidence/documentation and update workflow path triggers;
they do not change the qualified execution code.

- `identities.json` and `build-commands.json`: exact optimized ELF/debug
  identities, SHA-256 and separate-debug matches, with compiler arguments.
- `runs/*-{index,time}/`: unmodified raw stop snapshots, Activation
  observations and `selection-stops.json` for ADAS: both selected Activations
  retain earlier live lead/hazard inputs plus entry/decision/return state,
  raw locals, stacks, source locations and process/thread identities.
  Unavailable fields remain explicit. The BUS group captures record three
  distinct FMU instances in one stopped child process during one Step.
- `runs/*-index/recording.mcap`: one unmodified Recording per workload form.
  Before removing duplicate copies, all 24 retained Recordings were compared
  byte-for-byte within their form and checked against `report.json` hashes:
  five equal Recordings per ADAS form and four for the BUS group. The report
  also records the diagnostics-only comparison. Repeating the full comparison
  requires the workflow artifact; the repository keeps each comparison's
  hash and outcome plus five canonical Recordings.
- `report.json`: all Run outcomes, deadlines, cleanup observations, Recording
  hashes and equivalence results. Repeated launch command arrays are omitted;
  the full report is in the artifact. Per-capture `result.json` files duplicate
  this report and are omitted, as are successful debugger exit sidecars.
- `controls.json`: original actions, debugger outcomes and nonempty error/debugger
  logs from the 15 failed controls plus grouped rejection, with source hashes.
  The report already retains their Run outcomes; repeated results and detailed
  performance diagnostics are omitted. JSON whitespace is not retained in this
  consolidation. Native inferior kill has no runner report; `report.json`
  explicitly records that absence rather than inventing a functional exit code.
- `attach.json`: actual sibling attach refusal and ptrace_scope. No host
  permission setting changed.

From the repository root, without GDB or workload binaries:

```sh
.venv/bin/python -m pytest tests/test_source_debugging_proof.py
.venv/bin/python proofs/source-debugging/validate.py \
  proofs/source-debugging/evidence/runs/native-index native
.venv/bin/python proofs/source-debugging/validate.py \
  proofs/source-debugging/evidence/runs/bus-group-index bus-group
# Repeat for native/process/fmu/shim/bus-group and index/time.
```

`digests.txt` hashes this retained set. The full artifact includes its own hash
manifest; all of those hashes were verified before selecting this subset.
The retained set omits executable/debug binaries, copied sources, input
Recordings, provenance and package inventory; fetch the full artifact before
its expiry for those. Identities alone do not enable later re-symbolization.
These files support offline inspection and verification of the canonical
Recording hashes, not variable history, reverse execution or checkpoints.
PR and issue status are tracked in GitHub.
