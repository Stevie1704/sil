# Issue #155 deterministic CAN fault qualification

This evidence records the explicit fault schedule, FMI-LS-BUS notifications,
automatic retry behavior and separate receiver-delivery suppression experiment
added for issue #155. The schedule and bus implementation are first-party
model behavior. `FaultAwareSender.fmu` and `FaultAwareReceiver.fmu` are named,
adapted fixtures derived from the pinned external node; the unmodified upstream
fixtures remain separate.

The retained qualification was run on Linux x86-64 in the pinned Docker image
`sha256:666872fc064ec656ae7b4599396e49c284960d79f39730675045ea96f43d8e01`
using Python 3.13.7, FMPy 0.3.32 and GNU C++ 12.2.0. All 80 CAN qualification
pytest cases passed, including the independent FMPy traces, SiL Runs with the
adapted external nodes and deterministic repeated Recordings. The separate
UBSan core test also passed.
The qualification report binds the committed source inputs, FMUs, Manifests,
Recordings and runner by SHA-256. The bus FMU embeds clean source revision
`cf1482db4d6c60e878bab2bba9e7fac00126a897` with `source_dirty: false`.

`issue155-independent.json` stores an independent expected event table beside
the observed operation bytes for the no-fault baseline, scheduled errors and
retry, retry exhaustion, equal-time co-transmission, both rule precedence
orders, absent matches and receiver suppression. Expected frame-end times are
computed with the independent `tests/wire.py` reference, rather than copied
from the bus output. `issue155-traces.json` does the same for SiL Recordings
from the adapted external-node fixtures.

The full SiL Manifests, including each `--start` schedule value, are retained
as `issue155-fault-recovery.json`, `issue155-retry-exhaustion.json`,
`issue155-identical-co-transmit.json`, `issue155-receiver-suppression.json`,
`issue155-overlap-suppression-first.json`,
`issue155-overlap-error-first.json`, and `issue155-absent-match.json`.
`issue155-identities.json` retains the embedded source identities and SHA-256
for the bus FMU and all upstream-derived node fixtures. `qualification.json`
hashes these identities and Manifests with their Recordings, FMUs, source
inputs and runner. The single-error retry completes after the official
wire-length reference plus the model's three-bit retry gap; retry exhaustion
emits the same specified Bus Error before dropping the request. In suppression,
the sender gets Confirm while the selected receiver gets no payload, and the
later request is delivered normally.
`qualification.json` retains artifact and input digests plus the qualified
fault and error-confinement scope. The full generated FMUs and Run files remain
in `build/can/`; regenerate the suite with `models/can/run.sh`.

A scheduled transmission error produces the 15-byte Bit Error Bus Error at the
nominal end of the failed frame, using the operation layout and flags in the
official [FMI-LS-BUS 1.0.0 Network Abstraction specification, Tables 14–16](https://fmi-standard.org/fmi-ls-bus/1.0.0/).
The rule-selected terminal is PRIMARY and is the sole terminal marked as the
sender; all others, including co-transmitters, are SECONDARY with the sender
bit clear. The original Confirm and payload are suppressed. The bus owns and
retries each accepted request up to the configured finite limit, preserving
its original request time and occurrence. The fixture parser is first-party
test code and does not provide independent implementation compatibility
evidence. Receiver suppression omits only that receiver's successful payload
delivery and creates no Bus Error or retry. No electrical fault waveform, CAN
error counter, error-active/passive state, or bus-off behavior is claimed.
