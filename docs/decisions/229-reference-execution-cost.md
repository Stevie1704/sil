# Reference execution cost: baseline, no framework change (#229)

Accepted 2026-10-01. Decision: keep the measured execution cost of the ADAS
reference controller as a **reference baseline** and change no framework
contract. This resolves
[#229](https://github.com/Stevie1704/sil/issues/229). The procedure, the
retained results and their reading are in
[proofs/adas-cost/](../../proofs/adas-cost/README.md).

## Why

[#125](https://github.com/Stevie1704/sil/issues/125) gates any Step-transport
work on the measured Step cost of a real vECU. No vECU is available. A
repeatable measurement of a repository-owned workload, with deterministic
counters apart from observational timings, is the procedure that the real
measurement will reuse.

## What was measured

One declared workload through three forms of the same C sources: the Native
participant, a process-isolated C-library adapter (`ctypes`, Step protocol)
and the FMI Importer, on Linux x86-64. On the retained run, the application
computes in 1.4 µs per Step; the native form costs 2.9 µs per Step and the
Process forms 47–87 µs; a Process instance starts in 50–76 ms. All forms
publish byte-identical Commands, and the instrumented Runs write the same
Recording bytes as the production Runs.

## What this decision is not

- It does not close #125 and does not choose a lever. #125 still requires a
  real vECU's Step cost; this reference application is not one.
- It is not a capacity claim and sets no throughput threshold.
- It changes no contract: the Manifest, Manifest hash, Step protocol, Arena
  layout, Native participant ABI, Recording bytes and exit codes stay as
  they are. ADRs 0001–0003 are unchanged.

## Consequences

- The ADAS reference example gains `process_adapter.py`, a third controller
  form with the same config and the same Commands as the Native form.
- Any future optimization needs a separate bounded issue, justified by real
  target measurements, that preserves what every Participant sees and the
  output semantics.
