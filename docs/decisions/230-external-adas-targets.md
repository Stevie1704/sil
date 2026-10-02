# First external C target and FMU target (#230)

Accepted 2026-10-02. Decision: select **two separate public targets** for
the external adoption milestone. They are not two forms of one function, and
no equivalence between them is claimed. This records the selection for
[#230](https://github.com/Stevie1704/sil/issues/230). #230 stays open until
the remaining pins and the static FMU inspection exist (see
[Status](#status-of-230)).

| Role | Target | Input kind | Licence |
| --- | --- | --- | --- |
| External C application, for [#232](https://github.com/Stevie1704/sil/issues/232) | opendbc safety logic (`opendbc/safety/`), built by its `libsafety` harness | Encoded CAN frames | MIT |
| External FMU, for [#233](https://github.com/Stevie1704/sil/issues/233) | `OSMPDummySensor` from OSI Sensor Model Packaging | Processed objects (OSI ground truth) | MPL-2.0 |

## Why

No company or supplier artifact is available, and the maintainer works on
SiL privately. The targets must therefore be public. They must also be usable
later inside a company, so every artifact and every recording must permit
commercial use. This excludes non-commercial datasets (highD/inD/exiD,
nuScenes, Argoverse 2, Waymo Open) as a regression baseline. Their terms do
not affect SiL's Apache-2.0 licence, but a company regression run is
commercial use.

The two targets cover both input kinds that #230 asks to identify: encoded
vehicle-network messages and processed objects. Both are written by third
parties, not in this repository.

Rejected:

- [autowarefoundation/vision_pilot](https://github.com/autowarefoundation/vision_pilot):
  its input is raw camera images (that is #192 scope), and it supplies no test
  data of its own.
- A single function in both forms: no public ADAS function exists as the same
  implementation in C and as an FMU.
- An FMI 3.0 port of `OSMPDummySensor`: OSMP 1.6 permits FMI 3.0 Binary
  variables, but the upstream example is FMI 2.0 only. A port made here would
  be repository-authored and would no longer be independent evidence. It is
  the fallback if #191 is declined.

## C target: opendbc safety

The same artifact that #178 qualified as the public shared-library workload.
See [proofs/public-workloads](../../proofs/public-workloads/README.md) and
[proofs/libsafety](../../proofs/libsafety/).

| Item | Value |
| --- | --- |
| Source | [commaai/opendbc](https://github.com/commaai/opendbc), commit `c4465696dc2ef0d1340ae188810ac982586999a4`, tree `760d44fd` |
| Build | upstream `libsafety_py._build_libsafety(release=True)`, during preparation only. Runtime needs `libubsan` |
| Function | Vehicle safety envelope: controls engagement, driver override, actuator command limits, with one mode per vehicle brand |
| C interface | `set_safety_hooks(mode, param)`, `set_alternative_experience`, `set_timer`, `safety_tick`, `safety_config_valid`, `safety_fwd_hook`, `safety_rx_hook`, `safety_tx_hook` |
| Input | Encoded CAN frames: bus, address, 1 to 8 payload bytes. The library decodes and checks them itself (checksums, counters), so SiL needs no DBC |
| Time | Only `set_timer` (µs, modulo `0xFFFFFFFF`). No other clock, no threads, no callbacks, so it can run entirely from Virtual time |
| State | C globals. One instance per process. No termination call |
| Recording | commaCarSegments `df5ad9d9ae9fd6cd/00000470--cb630a6b9d/46`, `TOYOTA_RAV4_TSS2`, dataset revision `edb6480d`, MIT. 6000 `can` events in 60 s, 7 to 47 frames per event |
| Expected slice | Upstream `replay_drive` on the same pins, plus the vehicle's recorded `pandaStates.controlsAllowed` as supporting evidence |
| Outputs | `controls_allowed`, receive validity per frame, `safety_config_valid`, and `safety_tx_hook` accept/reject |

Gaps that #232 must close:

- **Transmit coverage.** The pinned segment has no `sendcan`. #232 selects a
  public route with `sendcan`, or declares generated transmit messages.
- **Native participant.** #178 proved only a process-isolated adapter. With
  global state and one instance per process, an in-process Native run is
  possible for one instance. #232 must try it.

## FMU target: OSMPDummySensor

| Item | Value |
| --- | --- |
| Source | [OpenSimulationInterface/osi-sensor-model-packaging](https://github.com/OpenSimulationInterface/osi-sensor-model-packaging) `v1.6.0`, commit `9fe6d0b1d3328667d228361d807a88a61727534e`, `examples/OSMPDummySensor` |
| Licence | MPL-2.0. OSI itself is MPL-2.0 |
| Build | Upstream CMake with Protobuf and OSI (`examples/osi-cpp` submodule). No prebuilt Linux x86-64 binary exists; build it unmodified on the supported platform and pin the compiler, Protobuf and OSI versions |
| FMI | FMI 2.0 Co-Simulation |
| Interface | OSMP binary variables. Each one is three `fmi2Integer` variables: `base.lo`, `base.hi` (a pointer in two 32-bit halves) and `size`. Input `OSMPSensorViewIn` (serialized `osi3::SensorView`), output `OSMPSensorDataOut` (`osi3::SensorData`), plus a `SensorViewConfiguration` request and parameter |
| Function | Transforms ground-truth moving objects into the ego frame. Reports the objects within 1.1 × nominal range (default 135 m) and a ±30° cone, with an existence probability from distance |
| Period | 20 ms, declared in the configuration request |
| State | Per instance. No threads, no clock, no randomness |
| Input generator | `OSMPDummySource` from the same commit: 10 vehicles in closed-form motion from simulation time. It is deterministic and synthetic. Label it as such |
| Expected slice | Computed independently from the closed-form source motion and the sensor's documented geometry |

Gaps that #233 must close:

- **FMI 2.0** → [#191](https://github.com/Stevie1704/sil/issues/191).
- **OSMP pointer-in-Integer binaries.** The Importer maps FMI 3.0 Binary
  variables, not memory addresses in Integer variables. This is a separate
  edge capability ([#244](https://github.com/Stevie1704/sil/issues/244)). It requires the
  FMU in the Importer's address space, which is the existing placement under
  [ADR 0001](../adr/0001-connected-fmus-in-one-process-participant.md).
- **Not needed:** #199 (no Clocks or events), #192 (no raw data), #188.

## Follow-on blockers

| Issue | Selected blockers | Not needed |
| --- | --- | --- |
| #232 | none beyond the existing #230 and #228 | #188, #231, #201, #192 |
| #233 | #191 and #244 | #199, #192, #188 |
| #231 | stays `needs-info`: no Ethernet application protocol is selected | |

#201 (modeled CAN behavior) is not a blocker for #232. The library consumes
recorded frames directly. #201 applies only if a later experiment puts these
frames through the maintained CAN bus model.

## Status of #230

| Acceptance item | Status |
| --- | --- |
| Pins, licences, digests | C target pinned by #178. FMU source pinned above. FMU binary digest, exporter toolchain and runtime dependencies still open |
| C headers and runtime contract | Done above and in proofs/public-workloads |
| FMU inspection | Open: build the FMU and retain a static compatibility report |
| Recording or generator, expected slice | C target done (receive side). FMU generator selected. Expected slice still open |
| CAN or Ethernet definition | CAN: frames are consumed encoded and the library is the decoder. Capture format: openpilot `rlog`. No Ethernet |
| Throughput expectations | C target: 6000 events in 60 s, one participant. FMU: 20 ms period, 10 objects. Feed both into #125 |
| Follow-on blockers | Selected above |

## What this decision is not

- It is not supplier acceptance. Neither target is a production ADAS function,
  and `OSMPDummySensor` is a demonstration sensor model.
- It changes no contract. Manifest, Step protocol, Arena, Native participant
  ABI, Recording bytes and ADRs 0001–0003 stay as they are.
- It does not select a vendor, an industrial recording format or a production
  stale-data policy.
