# SiL Framework

Deterministic software-in-the-loop kernel for ADAS/AD regression testing:
**deterministic scheduling + typed data routing**, nothing else in the core.
Same artifacts + same machine class ⇒ bit-identical recordings. See
[DESIGN.md](DESIGN.md) for the decision record.

## Milestone 1 — walking skeleton + determinism proof

One run = `sil-run manifest.json -o out.mcap`:

- **Kernel** (C++20): central virtual-time master, two-tier scheduling
  (periodic tasks for native code, `step(t, Δt)` for opaque vECUs), typed
  pub/sub channels with explicit per-channel latency (default: unit delay —
  in-slot execution order cannot change outputs).
- **Native participants**: shared libraries against a small stable C ABI
  ([include/sil/participant.h](include/sil/participant.h)), loaded via the
  manifest.
- **Out-of-process participants**: any executable speaking a JSON-lines
  step protocol on stdin/stdout; `sil.participant` provides the Python side.
- **Recording**: uncompressed MCAP, virtual timestamps only, manifest hash
  embedded; bit-diff of two runs = determinism check.
- **Manifest**: canonical JSON, SHA-256 hashed, the single execution input.
  Built and validated with the `sil.manifest` Python builder.
- **Test API**: tests are scheduled participants (`sil.testing` +
  `sil.participant`) run under pytest; an in-simulation assertion failure
  aborts the run non-zero and fails the pytest test with that message.
- **Determinism check**: `sil-check manifest.json --runner build/sil-run`
  runs twice and bit-compares (exit 3 on violation). Enforced in CI.

Exit codes: `0` ok, `1` run/test failure, `2` config error, `3` determinism
violation (sil-check).

## Build & test

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
uv venv .venv && uv pip install -p .venv/bin/python -e "python/[dev]"
.venv/bin/python -m pytest tests/
```

The pytest suite builds the kernel if needed and includes the milestone exit
criterion (`tests/test_determinism.py`: run twice → bit-identical MCAP).

## Layout

```
kernel/src/        C++20 kernel: manifest, engine (scheduler+router),
                   recorder, native/process participant adapters
include/sil/       stable C ABI for native participants
participants/      toy native participants (walking-skeleton fixtures)
schemas/           message schemas (single typed contract)
tools/silschema.py schema → packed C structs; sil.schema packs the same
                   layout in Python
python/src/sil/    manifest builder, step-participant lib, test API,
                   determinism check
tests/             behavior tests at the run boundary
```

## Notes / deferred (per DESIGN.md)

- Shared-memory zero-copy payloads, bus adapters, FMI importer, replayer,
  clock shim for POSIX vECUs: later milestones.
- The recorder is fed in global publish order — behaviorally identical to a
  latency-0 subscriber scheduled last in every slot.
- Message layout is packed little-endian; cross-platform bit-exactness is an
  explicit non-goal.
