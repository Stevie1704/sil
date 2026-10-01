# Native ADAS reference application (#222)

Accepted 2026-10-01. Decision: add a repository-owned C radar/camera
reference controller as **intentional new example coverage** of the Native
participant ABI. This resolves the product question of
[#222](https://github.com/Stevie1704/sil/issues/222). The example is
[examples/adas-reference/](../../examples/adas-reference/); its profile is
[docs/adas-reference.md](../adas-reference.md).

## Why

No existing example proves the full path C application → Native participant
→ Recording → independent comparison. The shared-library example runs a C
library as a Process participant. The ABI's own fixtures are C++ toys with no
reference trajectory. Later array and FMI work needs an executable contract
to measure equivalence against. A small, versioned reference profile is that
contract.

## What this decision is not

- It is not an external consumer. No supplier application or message
  database exists. The example does not satisfy the reopening evidence of
  [#118](118-consumer-capability-gate.md) or
  [#86](https://github.com/Stevie1704/sil/issues/86), and it does not reopen
  either one. Its friction is not evidence for a new framework capability.
- It makes no vendor, perception-accuracy or safety claim. Its constants
  define test behavior, not vehicle requirements.
- It changes no contract. The Manifest, Manifest hash, Step protocol, Arena
  layout, Native participant ABI, Recording format, exit codes and supported
  machine class stay as they are. Existing successful Runs keep their
  Recording bytes. ADRs 0001–0003 are unchanged: FMI coordination stays in
  the Importer, vehicle-network semantics stay at the edge.

## Consequences

- The application has its own C API and includes no SiL header. Only the
  adapter exports `sil_participant_init`.
- Native ABI v1 has no termination callback. The adapter keeps one
  controller per Participant for the Run's lifetime and documents that;
  it does not add a callback.
- The CI determinism gate and the test suite run the example on every push.
