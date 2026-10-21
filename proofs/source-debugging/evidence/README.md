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
  observations, capture result and Recording. The ADAS captures include
  debugger outcome and `selection-stops.json`: both selected Activations
  retain earlier live lead/hazard inputs plus entry/decision/return state,
  raw locals, stacks, source locations and process/thread identities.
  Unavailable fields remain explicit. The BUS group captures record three
  distinct FMU instances in one stopped child process during one Step.
- `runs/*-{ordinary-0,ordinary-1,detach}/`: unmodified successful Recordings.
  Each ADAS form's five saved Recordings compare byte-for-byte. The BUS group
  has four equal Recordings (two ordinary plus index/time captures). The additional
  diagnostics-only equality result is in `report.json` and the full artifact.
- `controls.json`: decoded JSON and exact text from the 15 failed-control
  captures plus grouped rejection. Each original file's SHA-256 is retained.
  JSON whitespace is not retained in this consolidation. Native inferior
  kill has no runner report; that absence is explicitly recorded, rather than
  inventing a functional exit code.
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

`digests.txt` hashes this retained set. `capture-digests.txt` is the original
full-capture hash manifest, whose entries refer to files in the downloadable
artifact. All of its hashes were verified before selecting this subset.
The retained set omits executable/debug binaries, copied sources, input
Recordings, provenance and package inventory; fetch the full artifact before
its expiry for those. Identities alone do not enable later re-symbolization.
These files support offline inspection and Recording equality, not variable
history, reverse execution or checkpoints. PR and issue status are tracked in GitHub.
