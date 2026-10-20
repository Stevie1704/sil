# Retained Linux evidence

Captured by [Actions run 39466725936](https://github.com/Stevie1704/sil/actions/runs/39466725936)
for PR head `9718afe5a55f6178f4499b729d41fbe63ca9ae9a` on ubuntu-24.04.
`report.json` consolidates the capture identity, actual checkout merge revision,
all 19 Run outcomes/timings, Recording and binary hashes, build commands,
source identities, relevant package versions and selected compiler remarks.
Its GCC DWARF counts were extracted from the matching binaries identified
by those hashes. The six XRay DSO outcomes, including zero events and
NOT_INITIALIZED, remain in `results.runs[].trace`.

Only the raw files needed to independently check the conclusions remain:

- `explicit-0/`: one complete trace and its Activation join inputs; covers
  both object counts, every call and all 40 Activations.
- `loss-0/`: the seven-event trace, loss metadata and matching Activations;
  the decoder regenerates the partial report.
- `diagnostics-0/observations.jsonl`: the baseline for Activation overhead.
- `control.bin`, its metadata and map: verify XRay recursion, threads, tail
  exit and unmatched throw entry against actual function IDs.
- The two XRay DSO maps: distinguish optimized inline coverage from the
  noinline variant.

Raw events contain only function IDs, entry/exit kinds, host timestamps and
TIDs. Regenerate per-Activation counts, order and duration statistics:

```sh
python3 proofs/function-tracing/analyze.py \
  proofs/function-tracing/evidence/explicit-0/events.bin \
  proofs/function-tracing/evidence/explicit-0/observations.jsonl \
  build/function-tracing-calls.json
```

`digests.txt` checks every retained file. Derived summaries, empty XRay event
files, redundant observation streams, the generated Manifest, full compiler
logs and the complete host package inventory are omitted from Git. The full
workflow artifact `function-tracing-evidence` retains them, the matching
libraries/DWARF, copied sources and Recordings for 30 days. The retained
identity subset alone cannot re-symbolize an old binary after that artifact
expires; source-scope IDs remain decodable with the proof's fixed map. Repeat
the proof for a fresh complete executable/debug bundle.
