# Issue #156: first-party bus live/replay equivalence

`test_replay_equivalence.py` generated these artifacts in the CAN qualification
image on Linux x86-64. The exact image ID is in `issue156-report.json` and in
`build/can/image.txt` when reproduced. The runner revision, digest and
command-file digests are in each
`*.provenance.json`; `issue156-report.json` binds the retained Manifests,
Recordings, provenance files and FMUs by SHA-256. The report also records the
source revision and the digest of the test that generated the evidence.
The full CAN qualification completed with 81 passing tests.

Both cases use two CAN terminals and the first-party `SilCanBus.fmu`. Node1 is
the adapted `FaultAwareSender.fmu`, which emits identifier 1. Node2 is the
separately named `ContendingSender.fmu`, derived from the same pinned node with
an additional documented patch that emits identifier 2. The live Manifest
observes each node's outgoing terminal and both bus outputs. The replay
Manifest removes node1 but keeps bus.Node1, its stable rule
identity, bus configuration, other FMUs and bindings. A Replay participant
publishes the recorded `node1` Channel with zero Channel Latency into
`bus.Node1.Rx_Data`. Its stated FMI event time controls activation, including
an activation at the end of a Step. The retained participants cannot tell
whether their input came from the live node or that Recording.

The `contention` case uses 500 ms Steps. Both senders offer operations at
500 ms and 1000 ms. Their 1000 ms activations each carry two operations.
Identifier 1 wins arbitration; identifier 2 loses, stays queued, then
transmits after the winner. The bus completions are separate instants inside
the next outer Step, calculated from `tests/wire.py` rather than read back as
expectations from the Recording. The `scheduled-error` case uses 1 ms Steps
and a rule selecting terminal Node1, identifier 1, first occurrence, first
attempt within the inclusive 300–600 ms request window. The bus emits one Bus
Error, retries Node1, then transmits Node2's queued operation. The later Node1
request succeeds. This demonstrates rule matching and equivalent one-shot
observable behavior; the later request has occurrence 2, so the trace alone
cannot isolate the model's internal consumed flag from its occurrence criterion.
The Manifest retains the exact CAN model fault schedule and retry limit.

`issue156-report.json` compares every shared Channel in Publish order, by
operation payload and stated FMI event time. It does not compare whole
Recording bytes across different Manifests. Each successful live and replay
Manifest was also run twice and its own Recording bytes compared. The
`intercept-dropped` and `intercept-retimed` Manifests use hashed Interceptors and
successfully run, but their boundary and retained Channels fail equivalence.
They each pass a separate Determinism check. `intercept-unreachable` moves a
recorded activation behind its arrival Step; the Importer reports Run failure
(exit 1) with the stated instant and Step bounds.
The Manifest and exact diagnostic are retained; the partial failed Recording
is not needed. The negative replay Recordings are retained so their comparisons
can be rerun from evidence. Repeated successful Recordings had the same SHA-256
as their respective first Runs; the report records both digests without storing
duplicate copies.

The claim has a fixed-input boundary. Changing a closed-loop receiver could
change the removed node's future output; this Recording would then no longer
represent that changed system. No equivalence across changed physical inputs
is claimed.

This evidence is specific to the first-party bus and its explicit rule
configuration. The earlier upstream demonstration and its recordings remain
separate under [`proofs/fmi-ls-bus/`](../../../../proofs/fmi-ls-bus/README.md).
Its eight retained Recording digests and three positive replay comparisons are
rechecked by the same test, without rebuilding the upstream FMUs.
Issue 155's fault qualification remains under [`issue-155/`](../issue-155/README.md).

To regenerate, run `models/can/run.sh`. The `build/can/issue156-*` files include
the omitted repeat Recordings and routine logs. The CAN CI workflow runs the
same test through `models/can/qualify.sh`.
