# Retained Linux evidence

Captured by [Actions run 39466725936](https://github.com/Stevie1704/sil/actions/runs/39466725936)
from commit `9718afe5a55f6178f4499b729d41fbe63ca9ae9a` on ubuntu-24.04.
`capture.json` identifies the run; `results.json` records all 19 fresh Runs,
loaded-library/runner hashes and equal Recording hashes. `summary.json` gives
observed counts, entry order and host durations without timing thresholds.

The compact retained subset includes one complete explicit trace, the
exhaustion trace, both loaded-DSO XRay outcomes, the executable control,
Activation observations, compiler inline remarks/maps, complete package
inventory and build/source identities. Raw records contain only function IDs,
entry/exit kinds, host timestamps and TIDs. Decode a per-Activation report:

```sh
python3 proofs/function-tracing/analyze.py \
  proofs/function-tracing/evidence/explicit-0/events.bin \
  proofs/function-tracing/evidence/explicit-0/observations.jsonl \
  build/function-tracing-calls.json
```

`digests.txt` checks this retained subset. `full-capture-digests.txt` is the
original full artifact's hash inventory, with paths relative to that capture;
it is not a manifest of files present in this subset. The workflow artifact
`function-tracing-evidence` retains the matching libraries, DWARF, copied
instrumented sources, generated headers, input/output Recordings, remaining
repeats and full normalized call reports for 30 days. This retained identity
subset alone cannot re-symbolize an old binary after that artifact expires;
the raw function IDs remain decodable with the proof's fixed source-scope map.
Repeat the proof for a fresh complete executable/debug bundle.
