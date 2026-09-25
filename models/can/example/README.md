# CAN bus example

One JSON file configures a CAN bus Run. You do not edit C++ source. The same
file drives a SiL Run and an independent FMPy run of `SilCanBus.fmu`.

| File | Purpose | Needs |
| --- | --- | --- |
| `example.json` | The shipped configuration | — |
| `scenario.py` | Validates a configuration against the archive's packaged limits. Compiles it into FMU start values and one operation buffer per node and instant. | Python standard library |
| `sil_run.py` | Writes the SiL Manifest, Runs it twice with deadlines, checks that the Recordings are identical and writes `trace.json` | installed SiL (`sil` and `sil-run`) |
| `stimulus.py` | The node Participant: publishes each request in the Step that holds it | installed SiL |
| `independent.py` | An FMI 3.0 master on FMPy that drives the FMU directly and writes the same trace | FMPy, no SiL |

## Configuration

| Key | Meaning | Where it goes |
| --- | --- | --- |
| `nodes` | One entry per active node, in terminal order. Each has `arbitration_loss`: `BufferAndRetransmit` or `DiscardAndNotify`. The length sets `activeNodeCount`. | FMU parameter; each node's Configuration operation at 0 ns |
| `bitrate` | CAN bitrate in bit/s that all nodes configure at 0 ns (10000–1000000, dividing 10^9) | Configuration operation |
| `per_node_queue_capacity` | Pending frames per node (1–64) | FMU parameter |
| `fault_retry_limit` | Automatic retries after a scheduled Bus Error (0–4) | FMU parameter |
| `faults` | Up to 8 rules. `kind` is `ScheduledTransmissionError` or `ReceiverDeliverySuppression`. The fields are those of the [fault schedule](../README.md#deterministic-can-model-fault-schedule), with 1-based nodes. | FMU parameters |
| `frames` | Requests: `node` (1-based), `at_ns`, 11-bit `identifier`, `data` as hex (0–8 bytes) | Rx operations |
| `duration_ns`, `step_period_ns` | Run length and the SiL Step period. The trace does not depend on the Step period. | Manifest |

`scenario.py` reads the packaging-time limits (terminal count and Binary
`maxSize`) from the archive and refuses a configuration that exceeds them.
It also refuses these items: a frame with more than 8 data bytes, a frame from
an inactive node, a request outside the duration and an unknown fault kind.
The FMU validates its own parameter ranges and the bitrate before the Run
starts. See
[RELEASE.md](../RELEASE.md#configuration-without-source-edits) for how
packaging-time limits differ from Run configuration.

## The shipped example

Three nodes at 500 kbit/s (bit time 2000 ns). Node3 discards a request that
loses arbitration. Node2 (`0x200`, 1 byte) and Node3 (`0x300`, no data) contend
at 1000 ns. Node1 requests `0x100` (2 bytes) at 500000 ns, and a scheduled
Bit Error hits its first attempt. The bus retries once.

| Instant (ns) | Node1 | Node2 | Node3 |
| --- | --- | --- | --- |
| 111000 | `0x200` frame | Confirm `0x200` | ArbitrationLost `0x300`, then the `0x200` frame |
| 628000 | Bus Error, primary, sender | Bus Error, secondary | Bus Error, secondary |
| 762000 | Confirm `0x100` | `0x100` frame | `0x100` frame |

`tests/test_example.py` derives these instants from the independent wire
model in `tests/wire.py`. It checks both paths against this table, and checks
a two-node variant at a 1000 ns Step period.

## Run it

In the qualification image (from the repository root):

```sh
python models/can/example/independent.py models/can/example/example.json \
    build/can/SilCanBus.fmu independent.json
python models/can/example/sil_run.py models/can/example/example.json \
    build/can/SilCanBus.fmu sil-example /opt/kernel/sil-run
```

From a clean installed SiL bundle compared with the independent path:

```sh
models/can/run.sh      # builds the FMU and the qualification image
models/can/bundle.sh   # evidence in build/can/bundle/
```
