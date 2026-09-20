# Consumer capability gate (#118)

Accepted 2026-09-20. Decision: **no new framework capability** is justified by
the completed [#117](https://github.com/Stevie1704/sil/issues/117) consumer Run.
This resolves [#118](https://github.com/Stevie1704/sil/issues/118). Keep the
existing esmini integration as a consumer-side Process participant. Correct
one separately scoped checker omission in
[#134](https://github.com/Stevie1704/sil/issues/134); no adapter or data-plane
implementation is hidden in this decision.

## Evidence identity

The consumer is esmini's unmodified `esminiLib`, upstream
[esmini/esmini](https://github.com/esmini/esmini), MPL-2.0, playing
`resources/xosc/alks_r157_cut_in_quick_brake.xosc`. This is a scenario-engine
integration through the Process-participant Step protocol, not an FMU or a
Native participant. The exact retained evidence is under
[`proofs/esmini/evidence`](https://github.com/Stevie1704/sil/tree/main/proofs/esmini/evidence).

| Identity | Value |
| --- | --- |
| esmini version | `v3.8.1`, build `6383`, revision `v3.8.1-0-19d26b68` |
| `esmini-bin_Linux.zip` SHA-256 | `05e6c9bb0f3156cef6f193c126b5ae340d42256da9c4a9eb7436d3b99dee9654` |
| `esmini-fullsrc.zip` SHA-256 | `dacb0d3c7e8919e27c456d5cbbc3458c07134fa6ce3f84800dc1f0473d77105e` |
| SiL release | `v0.1.0` |
| Runner image | `ghcr.io/stevie1704/sil@sha256:d67a254125fbe44467bf1354a04d19955815098d86a4c94c74f73ead6a943c0b` |
| Derived image ID | `sha256:8fa6a3540f54cb38ac985f4fb9b3e39554344ae449fc954d63abe5dfee93f34f` |
| Nominal Manifest SHA-256 | `fd33135e30fdec40dfd7d147fa236ff82ed02e3c5c37b15356eb2b81b7dc2757` |
| Failing Manifest SHA-256 | `738d7f2be8a7068d96014a0d3fd77b56fe34a0afb7ff9271f67cd0f3acfca8ce` |
| Both nominal Recordings SHA-256 | `4ff77017a1ff66bc9374229e803843eb6c7ed724047d697c8f83bc59236116c4` |
| Evidence machine class | Native Linux x86-64 (`linux/amd64`), supported glibc runner image |

`identity.txt`, `manifest-hashes.txt`, `determinism.txt`, and retained
`run-1.mcap` bind these identities to the proof. They describe the historical
v0.1.0 Run, not a claim that the current development checkout is that release.

## What the Run demonstrates

The 8 s Run uses a 10 ms Step period, 800 Slots, and two 72-byte Channels,
`esmini.Ego` and `esmini.Target`. Each publishes once per Slot (100 Messages/s,
Burst depth one) to one Subscriber route of capacity two, with overflow
`fail`. The declared route payload footprint is `2 × 2 × 72 = 288 B`, with no
Arena and no unbounded route. This is payload capacity, not total process
memory.

The nominal Run exits 0. The minimum freespace gap is
`0.35777506742172704 m` at `7.68 s`, above the `0.25 m` floor. At `6.35 s`,
the target has changed into the ego's lane and ego speed is
`6.080000000000254 m/s`, below `10 m/s`. Post-hoc checks include the final
published state, which default Latency leaves unseen by the in-run Test
participant at the Duration boundary. Ten upstream-reference samples pass;
the largest position deviation is below `4.5e-4 m`, within the `1e-2 m`
tolerance. A static but well-formed trajectory fails the encounter assertions.

The deliberately failing 2 m floor produces exit 1 and a diagnostic naming the
measured gap, threshold and Virtual time. An unloadable scenario produces
exit 2. Both nominal Recordings contain 1,600 Messages and are bit-identical,
191,911 bytes each. The Clock-shim control has zero differing object-state
readings out of 16,000; this scenario proves the Step integration, not that
wall-clock interception was necessary.

`observations.json`, `resources.json`, `footprint.txt`, `failing-variant.txt`,
`manifest-error.txt`, and `clock-shim-control.json` retain these results.
Observed wall-clock time is `0.663 s` and peak child RSS is `61,952,000 B`.
These are machine-dependent observations, not capacity or correctness claims.
Copy counts and route high-water counters were not captured: the instrumented
development runner is absent from the released image. No copy-cost claim can
be inferred from those missing measurements.

## Concrete friction and boundary assessment

The proof needed shared-library packaging, stdout redirection, log-file
suppression, explicit ctypes bindings, object-to-Channel mapping, a Duration
inside esmini's stop trigger, alignment of initial state to Virtual time zero,
absolute paths, and transcription/verification of upstream expectations.
All nine were solved by the consumer without changing SiL or esmini. The
[proof report](https://github.com/Stevie1704/sil/blob/main/proofs/esmini/README.md)
records each adaptation. None establishes a missing kernel contract.

One exercised workflow remains unnecessarily difficult: direct Runs use
`--participant-timeout-ms 30000`, but `sil-check` cannot forward that deadline
to its two Runs. The proof separately compared deadline-enabled Recordings;
the checker itself can still wait indefinitely on a stalled Participant.
The smallest correction is an optional checker argument forwarded to the
existing runner option, specified in #134.

Two unexercised scenarios require more evidence. The adapter's static
object-to-Channel mapping does not cover entities created or deleted during a
scenario. That is not proof that current contracts cannot express them:
fixed-capacity arrays plus an active count, or entity-update Messages with
explicit identity/lifecycle fields and bounded routes, are candidate consumer
representations. A named dynamic scenario must test ordering, overflow,
identity reuse and subscriber reconstruction before a Schema change is
justified. These alternatives have not been validated by #117. Likewise, the
proof read the C API and did not require OSI ground truth over UDP.

## Alternatives and evidence required to reopen

| Outcome | Decision and reopening evidence |
| --- | --- |
| No new framework capability | **Selected.** All required scenario behavior fits the released contracts. Reopen when a named consumer supplies a reproducible unmet scenario and an edge-only solution is shown insufficient. |
| New or generalized consumer-side adapter | Deferred; retain the existing proof adapter. Require another named integration or a required dynamic-entity scenario, with an end-to-end KPI. Try bounded arrays/entity updates before proposing dynamic Channels or variable-length Schemas. |
| Additional FMI variable types | Deferred: this artifact is not an FMU. Require a versioned FMU naming the exact unsupported types, required behavior, and a redistributable conformance fixture or permitted private acceptance artifact. Test an Importer-only change first. |
| FMI-LS-BUS | Deferred: no layered-standard traffic is exercised. Require a versioned artifact using named layered-standard behavior and a permitted end-to-end acceptance artifact; show why existing Importer adaptation is insufficient. |
| One named bus adapter | Deferred: no CAN, SOME/IP, DDS, or OSI-over-UDP consumer path is required here. Require one named bus/interface, one behavior set and a consuming Participant/KPI; establish deterministic mapping at the edge. Do not bundle multiple buses. |
| Leased/zero-copy payload routing | Deferred: 72-byte payloads, one subscriber per Channel and 288 B of route capacity show no need. Require a named workload with payload sizes, rates, Burst depths, fan-out, copy counts, declared footprint, peak memory and measured copying cost, plus why bounded routes and existing Arenas fail its requirement. Address #85's six-subscriber or binding-memory reopening gate and separate deterministic counters from wall-clock observations. |
| Native ABI revision | Deferred: this Run uses Process participants. Require behavior ABI v1 cannot express and address #86: instance state already fits `user`/`ctx` (checked by #88), repeated in-process Runs need more than an ABI change, and capability discovery needs an actual optional capability. |

The next evidence comes from an intended adopter's workload; manufacturing a
fixture solely to justify expansion does not open the gate. Step-transport
performance remains the separate decision in
[#125](https://github.com/Stevie1704/sil/issues/125), with its own real-vECU
measurement requirement.

## Compatibility and acceptance

This decision changes none of the Manifest, Manifest hash, Step protocol,
Arena layout, Native participant ABI, Recording bytes, exit-code taxonomy or
supported machine class. Deterministic Activation order, Channel Latency and
Publish order remain unchanged. Existing successful Runs keep their
bit-identical Recording behavior. No Manifest or Step field is introduced, so
the #62 evolution-policy choices (omit-default, always-emit with legacy
default, required field, or new version) are not applicable. Any reopened
proposal must make that choice if it changes either contract.

The end-to-end acceptance evidence for retaining the current contracts is the
completed #117 Run above: nominal KPI and upstream-reference success,
deliberate failure propagation, and repeated byte identity. #134 separately
requires the updated checker to run this consumer with a 30000 ms deadline
on both invocations, retain byte identity and pass the same post-hoc checks;
a stalled-Participant test must propagate exit 1 and the timeout diagnostic.
Omission preserves existing checker behavior. A deadline is a failure guard,
not a new trajectory semantics or benchmark threshold.

Preserve the original release proof and hashes. Record the identities of the
updated checker and runner for the follow-up validation separately; update a
release-only reproduction only when a containing release exists. Non-goals
are new adapters, expanded FMI support, variable-length Schemas, dynamic
Channels, leased routing, ABI changes, transport rewrites and performance
thresholds. #134 is one independently deliverable tooling issue depending
only on this decision; no implementation is required to close the gate.
