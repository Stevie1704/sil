# Issue #154 CAN arbitration qualification

This is the model and configuration evidence for source revision
`66da3a1fa120fb64d739d370936b310a808245ab`. It is separate from the
pre-arbitration evidence in the parent directory and from the unchanged
upstream proof in `proofs/fmi-ls-bus/`.

The Linux x86-64 qualification image was
`sha256:666872fc064ec656ae7b4599396e49c284960d79f39730675045ea96f43d8e01`.
Both controlled FMU builds were byte identical at SHA-256
`4a9ecbfcfead9496bf4f892274b15b6a5598b35e385ea27d27f34c41a3939e33`;
the embedded identity reports this revision with `source_dirty: false`.
The pinned external FMUs retain their previous artifact digests.

All 58 independent FMI and SiL qualification cases passed, along with the
Linux UBSan core test. The existing external sender/receiver smoke, wire
timing vectors and burst traces remain passing. The arbitration cases cover
three simultaneous nodes, staggered contention, buffer and discard policies,
per-node FIFO and capacity, equal-ID co-transmission and conflicting payload
rejection, single and four active nodes, input and declaration reorderings,
multi-operation buffers, completion-boundary arrivals and a finite priority
stream. A conflicting equal-ID Run exits 1 when those frames reach arbitration;
if both contenders are discarded by a lower ID, no conflicting frame is sent.
The two Runs of each new Manifest have byte-identical Recordings. The host
Importer group/Clock regression suites passed 97 tests.

[`arbitration.json`](arbitration.json) retains every three-node output's exact
event instant, terminal and operation bytes. IDs 2, 0 and 1 arrive at 1000 ns;
ID 0 completes at 401000 ns, and buffered ID 2 at 801000 ns. Node3's
`DiscardAndNotify` output contains `ArbitrationLost(1)` followed by ID 0's
frame. Reversing source and binding declaration order gives the same trace.
The separate FMI staggered-contention test asserts its exact expected trace
at the FMPy boundary. [`arbitration-forward.json`](arbitration-forward.json)
and [`arbitration-reverse.json`](arbitration-reverse.json) are the corresponding
Manifest artifacts. [`qualification.json`](qualification.json) hashes the FMUs,
Manifests, Recordings, logs, source inputs and runner. Full generated binaries
and logs remain in `build/can/` and are reproducible with `models/can/run.sh`.
