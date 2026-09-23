# Standalone Classical CAN smoke FMU

Maintained C++20 model product for issue #152. `src/bus.*` is independent of
FMI and SiL; `src/fmi.cpp` owns each instance and translates FMI calls. The
Linux x86-64 shared object requires the C++ runtime, with no SiL symbols,
wall-clock access, threads, environment configuration, or random state.

## Supported profile

Pinned specification: [FMI-LS-BUS 1.0.0](https://fmi-standard.org/fmi-ls-bus/1.0.0/),
release commit `8abdf039bfb994c794e4c15bce575cfc00a1ab6e`, Network Abstraction.
This is a deliberately restricted smoke profile, not full CAN/FMI conformance.

| Surface | Support and rejection |
| --- | --- |
| Topology | Two active terminals, `Node1` and `Node2`; each is a `org.fmi-ls-bus.network-terminal` using `org.fmi-ls-bus.transceiver` matching. Both are active receivers throughout the Run. |
| Encoding | Little-endian operation buffers per §§5.2 and 5.5.1. Complete buffers up to 2048 bytes; exact lengths checked before reading fields. |
| Transmit | `0x10`, 11-bit ID 0–2047, IDE=RTR=0, 0–8 data bytes. FD, XL, extended and remote frames rejected. |
| Configuration | `0x40`: CAN bitrate 100000 bit/s; arbitration policy BufferAndRetransmit (1). Both are also defaults. Repeating those settings is allowed. Other rates, kinds and policies rejected. Configuration is consumed, never forwarded. |
| Confirmation | `0x20` with matching ID goes only to the sender; unchanged Transmit operation goes only to the other active terminal at the same event time (§5.5.1.2.2). No loopback. |
| Concurrency | One outstanding request across the bus. A second request before completion, including a second operation in one buffer, fails. No arbitration or retransmission is implemented yet. |
| Other operations | All other opcodes, including Status, Wakeup and incoming Confirm, fail explicitly. No FD/XL, DBC, faults, electrical fidelity, or bus-off behavior. |
| Clocks | Triggered input Rx_Clock per terminal; countdown input Tx_Clock per terminal. Both Tx Clocks request 1/1000 seconds and must activate together. Fraction and decimal interval queries supported. No output Clocks. |
| FMI | FMI 3.0 Co-Simulation, Event Mode mandatory, variable communication steps, multiple instances, reset. Binary access and Clock activation in Event Mode; Binary values may be assigned repeatedly in Initialization Mode; these assignments do not activate Clocks or submit frames and are cleared on exit. Calculated output Binary values are empty and readable during initialization. No ME, SE, rollback, serialization, intermediate updates, derivatives, structural parameters or early return. Unsupported entry points return fmi3Error (unsupported instantiation returns null). |
| Invalid calls | Invalid references, lifecycle/order, missing Clock/Binary pairs, overflow, malformed/unsupported operations and stepping past a pending event return fmi3Error. Implemented calls put the instance in Error state, requiring reset/free; no C++ exception crosses the ABI. |

**Timing approximation:** every accepted frame completes exactly 1 ms after
its request, independent of ID, payload and bitrate. This is only an exchange
smoke test. It is not a CAN frame duration, bandwidth estimate, arbitration
model, bit-stuffing calculation or ADAS timing claim. The later transmission timing issue must replace this approximation before those uses.

## Build and qualify

From the repository root:

```sh
models/can/run.sh
```

Preparation uses Docker/network and pins the external node, released protocol
headers, Python base and FMPy. The subsequent test container has no network.
Artifacts and evidence are written to `build/can/`. The same `SilCanSmoke.fmu`
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
`proofs/fmi-ls-bus/` beta proof is preserved unchanged. No Importer or kernel
change is needed. ADRs 0001/0002 still govern event coordination and replay.
