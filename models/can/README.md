# Standalone Classical CAN bus FMU

Maintained C++20 model product (issues #152–#154). `src/bus.*` is independent of
FMI and SiL; `src/fmi.cpp` owns each instance and translates FMI calls. The
Linux x86-64 shared object requires the C++ runtime, with no SiL symbols,
wall-clock access, threads, environment configuration, or random state.

## Supported profile

Pinned specification: [FMI-LS-BUS 1.0.0](https://fmi-standard.org/fmi-ls-bus/1.0.0/),
release commit `8abdf039bfb994c794e4c15bce575cfc00a1ab6e`, Network Abstraction.
This is a deliberately restricted profile, not full CAN/FMI conformance.

| Surface | Support and rejection |
| --- | --- |
| Topology | Four terminals (`Node1`–`Node4`) are declared; `activeNodeCount` selects the first 1–4, default 2. An inactive terminal accepts no input and receives no operation. Every active terminal receives each successful frame unless it was a sender. |
| Encoding | Little-endian operation buffers per §§5.2 and 5.5.1. Complete buffers up to 2048 bytes; exact lengths checked before reading fields. |
| Transmit | `0x10`, 11-bit ID 0–2047, IDE=RTR=0, 0–8 data bytes. FD, XL, extended and remote frames rejected. |
| Configuration | `0x40`: CAN bitrate from 10000 to 1000000 bit/s dividing 10^9. All active terminals must agree before transmission. Each may select BufferAndRetransmit (1, default) or DiscardAndNotify (2); other rates, kinds and policies fail. Configuration is consumed, never forwarded. |
| FMI parameters | Fixed Float64 parameters `activeNodeCount` (value reference 1025, integer 1–4, default 2) and `perNodeQueueCapacity` (1026, integer 1–64, default 4) are set before Initialization Mode. The existing Importer `--start bus.activeNodeCount=3 --start bus.perNodeQueueCapacity=2` path configures them; no dynamic terminal creation or new Manifest field is needed. Invalid or fractional values fail. |
| Confirmation and delivery | At frame end, each sender receives `Confirm` (`0x20`) and each other active terminal receives one unchanged `CanTransmit`. No loopback. A discarded loser receives `ArbitrationLost` (`0x30`, its lost ID) followed by the winning frame in the same operation buffer at that frame end. |
| Arbitration and queue | All same-instant inputs are collected before arbitration. The lowest 11-bit identifier among each node's FIFO head wins. Each node's pending queue has the configured capacity; a frame already on the wire does not count. Full queues fail atomically and put the FMU in Error state, ending the Run. A wire frame cannot be preempted. After each frame and three-bit intermission, the heads compete again, including requests arriving at that boundary. BufferAndRetransmit leaves a loser queued; DiscardAndNotify removes its head and reports the loss. Distinct same-ID payloads fail when that ID wins, because electrical error behavior is outside the model; bit-identical same-ID frames co-transmit and all senders receive Confirm. |
| Other operations | All other opcodes, including Status, Wakeup and incoming Confirm, fail explicitly. No FD/XL, DBC, faults, electrical fidelity, or bus-off behavior. |
| Clocks | Triggered input Rx_Clock per terminal; countdown input Tx_Clock per terminal. Active Tx Clocks state the time to the next frame end or arbitration opportunity as `counter / 10^9` s and activate together. An arbitration countdown with no delivered operation has an empty Binary output, which the Importer consumes without propagating a frame. Qualifier `Changed` when the next bus event changes, `NotYetKnown` when none is pending. Fraction and decimal queries supported. |
| FMI | FMI 3.0 Co-Simulation, Event Mode mandatory, variable communication steps, multiple instances, reset. Binary access and Clock activation in Event Mode; Binary values may be assigned repeatedly in Initialization Mode; these assignments do not activate Clocks or submit frames and are cleared on exit. Calculated output Binary values are empty and readable during initialization. No ME, SE, rollback, serialization, intermediate updates, derivatives, structural parameters or early return. Unsupported entry points return fmi3Error (unsupported instantiation returns null). |
| Time | Start time and every communication point must be in [0, 2^50] ns (about 13 days); there a Float64 time converts to whole nanoseconds without loss. A Float64 time is read as the nearest whole nanosecond; the SiL Importer supplies whole nanoseconds. A frame that would end later fails. |
| Invalid calls | Invalid references, lifecycle/order, missing Clock/Binary pairs, overflow, malformed/unsupported operations, Tx activation away from a pending bus event and stepping past one return fmi3Error. Implemented calls put the instance in Error state, requiring reset/free; no C++ exception crosses the ABI. |

An empty queue schedules no event. One active node sends and confirms normally,
with no other receiver. Finite offered traffic drains after finitely many frame
ends unless an invalid buffer or equal-ID conflict fails the instance. CAN
priority supplies no fairness: an indefinitely renewed lower-ID head can keep
a higher-ID pending frame waiting indefinitely. The finite per-node capacity
bounds stored traffic, not waiting time.

FIFO applies within each node before CAN priority compares nodes: if one node
queues ID `0x300` and then ID `0`, its later ID `0` cannot compete until
`0x300` has transmitted. Equal-ID payloads are compared only when that ID wins
arbitration. If a lower ID wins first, those heads have not sent their data;
they remain queued or are both discarded under DiscardAndNotify, so their
different payloads cause no modeled error at that opportunity.

## Timing model

The bus is ideal and error free: every frame is acknowledged, and no error,
overload frame, suspend transmission, bit-timing segment or propagation delay
exists. Time is an integer nanosecond count; bit time `T = 10^9 / bitrate` ns.

For an 11-bit data frame with `n` data bytes:

    N(n)    = 44 + 8n + S              frame bits, SOF through the last EOF bit
    44      = SOF 1 + ID 11 + RTR 1 + IDE 1 + r0 1 + DLC 4 + CRC 15
              + CRC delimiter 1 + ACK slot 1 + ACK delimiter 1 + EOF 7
    S       = stuff bits in SOF .. CRC sequence (34 + 8n bits)
    end     = start + N(n) * T
    next    = end + 3 * T              first instant after intermission

**Bit stuffing is modeled exactly, not approximated.** The model builds the
unstuffed bit sequence from the frame's ID, DLC and data, computes the
ISO 11898-1 CRC-15 (polynomial `0x4599`, initial value 0) over SOF through
data, and counts the stuff bits: one complement after every five equal bits,
stuff bits counting towards the next run, including one after the last CRC bit.
So `N(n)` is the exact wire length of that frame on an error-free bus. It is
not a bit-accurate physical-layer simulation: the simplifications above and
the receive instant below remain.

| Event | Instant | Meaning |
| --- | --- | --- |
| Request | FMI event time of the Rx activation | The Transmit operation reaches the bus. |
| Start of transmission | request instant if the bus is idle, else an arbitration opportunity after intermission | SOF begins after eligible queue heads compete. It is not aligned to a bit grid. A frame on the wire is never preempted. |
| Frame end | `start + N(n) * T` | End of the last EOF bit. |
| Receive, confirmation and loss visibility | frame end | The bus activates every active Tx Clock: Confirm to senders, unchanged Transmit to other active nodes, and a preceding ArbitrationLost operation for each discarded contender. ISO 11898-1 lets a receiver accept a frame one bit earlier, at the last-but-one EOF bit; this model delivers it at most one bit time `T` later than that. |
| Intermission | frame end to `next` | Three recessive bits. A request that arrives in this period, or during the frame, starts at `next`. |

The bus asks for a frame end or an arbitration opportunity after intermission.
The latter is needed so newly arrived traffic can compete before SOF. After
each event that changes the next bus event, the countdown interval states it exactly. The
Importer rejects an interval that is no whole number of nanoseconds; with the
supported bitrates that cannot occur.

### Hand-derived timing vectors

ID 0 with all-zero data keeps the stuffing count checkable by hand. The CRC
values are the polynomial remainders; `tests/wire.py` computes them with an
independent method, and only their runs matter here.

| n | Unstuffed SOF .. data | CRC-15 | Stuff bits | N | 125 kbit/s (8000 ns) | 500 kbit/s (2000 ns) |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 19 zeros | `000000000000000` | 6 | 50 | 400000 ns | 100000 ns |
| 1 | 18 zeros, `1`, 8 zeros | `100010000100110` | 4 | 56 | 448000 ns | 112000 ns |
| 8 | 15 zeros, `1`, 67 zeros | `001010001011011` | 16 | 124 | 992000 ns | 248000 ns |

- n = 0: 34 equal zeros. A stuff bit follows original bits 5, 10, 15, 20, 25
  and 30; the last 4 zeros form no run of five. S = 6.
- n = 1: 18 zeros give 3 stuff bits and leave a run of 3. `1` ends it. The 8
  data zeros give 1 stuff bit and a run of 3, which the CRC's leading `1` ends.
  The CRC has no run of five. S = 4.
- n = 8: 15 zeros give 3 stuff bits; the last stuff bit (`1`) and DLC `1` form
  a run of 2. Then 3 DLC zeros and 64 data zeros form 67 zeros: 13 stuff bits,
  run of 2. The CRC's leading `00` extends that run to 4, and `1` ends it. The
  rest of the CRC has no run of five. S = 16.

`tests/test_exchange.py` runs these vectors at both bitrates through the FMI
boundary. It also runs a burst: Node1 requests A at 1 us, and B while A is on
the wire; Node2's C arrives while B is on the wire. At 500 kbit/s the expected
frame ends are 249000, 501000 and 673000 ns. They are checked against
`tests/wire.py`, on an independent FMPy master at four outer Step grids, and in
SiL Recordings at three Step periods.

The three-node fixture offers IDs 2, 0 and 1 at 1000 ns. ID 0 completes at
401000 ns; with Node3 configured to discard, it receives `ArbitrationLost(1)`
and that frame, while Node2 receives `Confirm(0)`. ID 2 is retained and
completes at 801000 ns. Independent FMI calls and SiL Recordings assert every
node's exact operation bytes and event time. Reversing same-instant input and
Manifest binding order leaves the trace unchanged; repeated Runs of each
Manifest produce identical Recording bytes. A second FMI fixture covers two
operations in one Binary buffer and a new request at a completion boundary.

The Importer change is confined to same-instant coordination: it drains non-bus FMU
events before handling a bus FMU, combines all operations for each bus terminal
into one Binary activation, and consumes an empty arbitration countdown
without forwarding an empty operation buffer. The three-node simultaneous
fixture requires this because sequential `UpdateDiscreteStates` calls would
otherwise let the first FMI callback start transmitting before the other
nodes' requests arrived. This changes only the Importer's ordering of an
instant; Manifest/hash, Step protocol, Arena, Native ABI, successful Recording
format and exit codes retain their contracts. The older upstream proof under
`proofs/fmi-ls-bus/` remains separate and unchanged.

## Build and qualify

From the repository root:

```sh
models/can/run.sh
```

Preparation uses Docker/network and pins the external node, released protocol
headers, Python base and FMPy. The subsequent test container has no network.
Artifacts and evidence are written to `build/can/`. The same `SilCanBus.fmu`
is loaded by independent FMPy calls and SiL's existing FMU group. Two controlled
FMU builds and two SiL Recordings are separately compared byte for byte.

The archive contains model/terminal/layered-standard XML, Linux library,
resources/identity.json, source files and build identity, and licenses. To build
only the model in the prepared image: `python models/can/build.py OUTPUT.fmu`.
The compiler command, Git revision, dirty-worktree flag and source digests are
recorded in its identity file. `profile.json` is the shared source for upstream
pins, instantiation token and the XML/C++ value-reference layout.
Inside an extracted archive, `sh sources/build.sh` rebuilds the shared library
with a Linux x86-64 C++20 compiler; Python/FMPy are not needed for that step.

## External node qualification

See [qualification/README.md](qualification/README.md). The old
`proofs/fmi-ls-bus/` beta proof is preserved unchanged. The Importer only
coordinates same-instant bus inputs; no kernel change is needed. ADRs 0001/0002
still govern event coordination and replay.
