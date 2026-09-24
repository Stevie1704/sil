# Issue 156: first-party bus live/replay equivalence

`test_replay_equivalence.py` generated these artifacts with the pinned CAN
qualification image `sha256:666872fc064ec656ae7b4599396e49c284960d79f39730675045ea96f43d8e01`
on Linux x86-64. The runner digest and command-file digests are in each
`*.provenance.json`; `issue156-report.json` binds all successful Manifests,
Recordings, provenance files and FMUs by SHA-256. The source tree began at
`6d64f66b581f6d3f4249fc38a2b368de972d4e74`, the merged issue 155 branch.

Both cases use three terminals: two pinned, adapted `FaultAwareSender` nodes,
one pinned, adapted `FaultAwareReceiver`, and the first-party `SilCanBus.fmu`.
The live Manifest observes each node's outgoing terminal and all three bus
outputs. The replay Manifest removes node1 but keeps bus.Node1, its stable rule
identity, bus configuration, other FMUs and bindings. A Replay participant
publishes the recorded `node1` Channel with zero Channel Latency into
`bus.Node1.Rx_Data`. Its stated FMI event time controls activation, including
an activation at the end of a Step. The retained participants cannot tell
whether their input came from the live node or that Recording.

The `contention` case uses 500 ms Steps. Both senders offer the same ID and
payload at 500 ms and 1000 ms; their 1000 ms activation carries two operations.
The bus completes those two frames at 1000830000 and 1001690000 ns, distinct
instants inside the next outer Step. The `fault` case uses 1 ms Steps and a
rule selecting sender terminal Node1, identifier 1, request 300 ms, occurrence
1, attempt 1. Both senders offer the same frame. The bus emits a Bus Error at
300830000 ns, then retries and completes at 301690000 ns. Its 600 ms request
succeeds without another error, demonstrating rule consumption with the source
removed. The Manifest retains the exact schedule and retry limit.

`issue156-report.json` compares every shared Channel in Publish order, by
operation payload and stated FMI event time. It does not compare whole
Recording bytes across different Manifests. Each successful live and replay
Manifest was also run twice and its own Recording bytes compared. The
`fault-dropped` and `fault-retimed` Manifests use hashed Interceptors and
successfully run, but their boundary and retained Channels fail equivalence.
`fault-unreachable` moves a recorded activation behind its arrival Step; the
Importer reports Run failure (exit 1) with the stated instant and Step bounds.
Its log, Manifest and provenance are retained. The repeat and negative
Recordings are retained as well, so all comparisons can be rerun from evidence.

The claim has a fixed-input boundary. Changing a closed-loop receiver could
change the removed node's future output; this Recording would then no longer
represent that changed system. No equivalence across changed physical inputs
is claimed.

This evidence is specific to the first-party bus and its explicit rule
configuration. The earlier upstream demonstration and its recordings remain
separate under [`proofs/fmi-ls-bus/`](../../../../proofs/fmi-ls-bus/README.md).
Issue 155's fault qualification remains under [`issue-155/`](../issue-155/README.md).

To regenerate the retained artifacts, run `models/can/run.sh` and copy the
`build/can/issue156-*` files into this directory. The CAN CI workflow runs the
same test through `models/can/qualify.sh`.
