# SilCanBus 1.0.0 release statement

`SilCanBus` is the first-party Classical CAN Bus Simulation FMU of SiL
(issues #152–#158). It is a source-available model product with its own
version, separate from the SiL runtime version. This statement tells you what
the release supports, how it was validated, and what it does not claim.
[README.md](README.md) is the full profile and timing specification; this
archive ships both files under `documentation/`.

## Not safety-qualified

**The FMU is not safety-qualified.** It supplies no safety case, no tool
qualification evidence and no tool confidence argument. It has no
ISO 26262 classification and no certification. A passing test with this model
says that the nodes behaved as the test asserted against this restricted bus
model. It says nothing about the safety of a vECU or of a real CAN network.
[SUPPORT.md](../../SUPPORT.md) applies to this model too.

## Identity

| Item | Value |
| --- | --- |
| Model | `SilCanBus`, version `1.0.0`, instantiation token `sil-can-bus-1` |
| Standard | FMI 3.0 Co-Simulation with Event Mode; FMI-LS-BUS 1.0.0 Network Abstraction, CAN, release commit `8abdf039bfb994c794e4c15bce575cfc00a1ab6e` |
| Released file | `SilCanBus-1.0.0-x86_64-linux.fmu` |
| Digests | `release.json` and `SHA256SUMS` beside the file |
| Build identity | `resources/identity.json` in the archive: Git revision, dirty flag, compiler, flags, FMI header source and the SHA-256 of every packaged source |

The CI workflow `can-model` uploads the release directory as the
`can-model-release` artifact. Verify a download with `sha256sum -c SHA256SUMS`.
`release.json` states the same identities and the qualification image. A
file is `releasable` only if it was built from a committed tree.

## Build and reproducibility

The shared library is C++20 and is compiled from the sources in the archive
(`sources/`). It links only the C and C++ runtime (`libstdc++`, `libm`,
`libgcc_s`, `libc`). It contains no SiL code and no Python runtime. Python and
FMPy are build tools: they write the XML and supply the pinned FMI 3.0 headers.
Inside an extracted archive, `sh sources/build.sh` rebuilds the library with
a Linux x86-64 C++20 compiler and no Python. With the qualification compiler
(GNU C++ 12.2.0), the result is byte-identical to the shipped library.

`models/can/run.sh` builds the FMU twice in the pinned qualification image.
`release.py` stages the release only if the two archives are byte-identical.
The archive packer fixes entry order, timestamps and permissions, so a
difference is unresolved build variability, not noise. A different compiler,
image or source revision makes a different artifact with a new digest.

## Supported platform

| Property | Supported |
| --- | --- |
| Operating system | Linux |
| CPU architecture | x86-64 (`binaries/x86_64-linux`) |
| C and C++ runtime | glibc and libstdc++ at Debian bookworm level (GCC 12) or newer |
| Importers | FMI 3.0 Co-Simulation importers with Event Mode, countdown and triggered Clocks, and Binary variables. The SiL FMU group and FMPy 0.3.32 are the qualified importers. |

Windows, macOS, arm64, Model Exchange and Scheduled Execution are not
supported. An arm64 host can run the x86-64 container under emulation for
development, but that is not a supported configuration.

## Configuration without source edits

A Run selects its bus without editing C++. Two kinds of limit exist:

| Kind | Items | Set by |
| --- | --- | --- |
| Packaging-time terminal limits | 4 declared terminals (`Node1`–`Node4`); Binary `maxSize` 2048 bytes per terminal; 8 fault rule slots; queue capacity at most 64; at most 4 automatic retries; 256 events at one instant | `profile.json` and the C++ source, fixed in the archive. A change is a new build. |
| Run configuration | active node count (1–4); per-node queue capacity; fault retry limit and fault rules | Fixed FMI parameters, set with `--start` in the SiL Manifest (hashed) or `fmi3SetFloat64` before Initialization Mode |
| Node configuration | bitrate; arbitration-loss policy (BufferAndRetransmit or DiscardAndNotify) | Each node's FMI-LS-BUS Configuration operation |

The data field of a Classical CAN frame is at most 8 bytes. The 2048-byte
`maxSize` limits one terminal's operation buffer for one event.

[`example/`](example/README.md) is a documented example. One JSON file selects
the node count, bitrate, arbitration policy per node, queue capacity, frames
and fault schedule. The example reads the packaging-time limits from the
archive's `modelDescription.xml` and refuses a configuration that exceeds them.
The same file drives a SiL Run and an independent FMPy run.

## Supported and unsupported CAN operations

Supported: 11-bit data frames with 0–8 data bytes (`CAN Transmit`), bitrate
and arbitration-loss Configuration, `Confirm`, `ArbitrationLost`, `Format
Error` for corrupt operations, and the scheduled `Bus Error` (Bit Error code).

Not supported, and the instance fails with `fmi3Error`: extended (29-bit) and
remote frames, CAN FD and CAN XL frames and bitrates, `Status`, `Wakeup`, and
incoming operations that only a bus produces. The model has no error
counters, no error-active, error-passive or bus-off states, no acknowledgement
failure, no overload frames and no physical layer. See the
[profile table](README.md#supported-profile) and
[malformed traffic](README.md#malformed-traffic-and-resource-bounds).

## Timing fidelity

Frame length is exact for an error-free bus: the model computes the CRC-15 and
the stuff bits of each frame. A frame ends `N(n) * T` after its start of frame,
and the next arbitration opportunity is three bit times later. Receive,
confirmation and loss become visible at the end of the last EOF bit. That is
at most one bit time later than the instant at which ISO 11898-1 lets a
receiver accept a frame. A scheduled Bus Error uses the nominal frame duration and
adds no error frame. The model has no bit-timing segments, no propagation
delay, no oscillator tolerance and no synchronization. See the
[timing model](README.md#timing-model).

## Validation

CI runs these checks on x86-64 Linux for every pull request:

| Check | Where |
| --- | --- |
| Exchange with pinned upstream external nodes, through independent FMPy calls and the SiL FMU group | `tests/test_exchange.py` |
| Hand-derived timing vectors and a burst at several Step grids | `tests/test_exchange.py` |
| Three-node contention, buffer and discard policies, input-order independence | `tests/test_exchange.py` |
| Deterministic fault schedule: Bus Error, retry, exhaustion, suppression, precedence | `tests/test_exchange.py` |
| Live/replay equivalence under contention and faults | `tests/test_replay_equivalence.py` |
| Lifecycle isolation, reset, interleaved instances and a malformed corpus under sanitizers | `tests/abi.cpp`, `tests/test_bounds.py` |
| Repeated Runs of one Manifest give identical Recording bytes | several tests |
| Two controlled builds are identical; release identity; linkage; rebuild from `sources/` | `tests/test_release.py` |
| The documented example against a hand-derived table on both paths | `tests/test_example.py` |
| The example from a clean installed SiL bundle, compared with an independent FMPy path | `bundle.sh` |

`bundle.sh` uses the unchanged production runtime image. SiL comes from the
installed wheel and `sil-run`; the container has no source tree and no
network. The independent path is FMPy 0.3.32 in the qualification image,
where `sil` cannot be imported. It does not reuse SiL's Importer. The two
traces must be equal. Both paths read one configuration through
`example/scenario.py`, so a fault in that encoding would affect both in the
same way. `tests/test_example.py` therefore also checks both traces against a
table derived by hand from the independent wire model. Each container has a deadline, and each Run and
Participant response has its own deadline. The retained evidence is under
[`evidence/issue-158/`](evidence/issue-158/README.md).

## Upstream beta evidence

The earlier FMI-LS-BUS beta proof under `proofs/fmi-ls-bus/` stays unchanged.
It qualifies the upstream beta example, not this model. The released-profile
evidence for this model is under `evidence/`, one directory for each issue.
New model and configuration artifacts get new digests and new directories. No
earlier file is renamed or re-labeled.

## Compatibility and maintenance policy

The model version follows semantic versioning. Within major version 1, these
stay stable: the instantiation token, the terminal and variable names, value
references, parameter meanings and defaults, and the operation bytes and
instants for the same inputs and configuration.

| Change | Version |
| --- | --- |
| Different operation bytes or instants for the same inputs, a removed or renamed variable, a new rejection of accepted input | Major |
| A new parameter or operation whose default keeps earlier traces | Minor |
| A fix that restores documented behavior, stated in the release notes with its trace impact | Patch |

Only the latest release gets fixes. Maintenance is best effort, with no fixed
response time. Report problems as GitHub issues in `Stevie1704/sil`.

The FMU keeps SiL's contracts: CAN semantics stay in the model, and the SiL
Importer only coordinates FMI events at its edge (ADR 0001, ADR 0002, ADR 0003).
The model changes no Manifest, hash, Step protocol, Arena layout, Native
participant ABI, Recording format or exit code.

## Licensing

The model, its build scripts and the example are Apache-2.0 (`LICENSE`,
`NOTICE`). The FMI 3.0 headers in `sources/` are BSD-2-Clause
(`documentation/licenses/FMI-BSD-2-Clause.txt`). The upstream external nodes
(BSD-2-Clause) are test fixtures only. The released archive does not contain
them.
