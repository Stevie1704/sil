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

## Object lists (#223)

Profile 2 replaces one object per sensor with bounded lists: flat Schema
arrays of capacity 8 and an active count. It reuses the existing fixed-size
Schema arrays; it adds no nested or variable-length Schema form. To keep
input preparation on `sil-csv` and its receipts, `sil-csv` converts a
fixed-size array field from one column per element. This is additive: a
scalar mapping and its Recording bytes do not change. The Manifest, Native
ABI, Step protocol, Recording format and `sil-compare` contract do not
change. The five profile 1 maneuvers keep their expected trajectories as
lists of at most one object.

## Sensor freshness and deterministic faults (#224)

Profile 3 replaces profile 2. Each sensor publishes at its own Period
(radar 20 ms, camera 40 ms, ego motion 10 ms in `cadence`); the controller
holds the last accepted observation of each sensor and judges freshness from
its Sample time, never from arrival. Sequences must increase within a Run;
duplicates and regressions are ignored and counted in the Command, while a
future Sample time fails the Run as malformed input. These are reference
policies for test coverage, not production stale-data requirements.

Faults use the existing mechanisms only: `drop`, `delay` and `override`
Interceptors, Channel Latency and finite Subscriber routes, each declared in
its own experiment Manifest and compared with its own enumerated
trajectory. The input routes hold three Messages, because a delayed Message
keeps its place in the route and later Messages wait behind it. The kernel's
Latency, Interceptor and route semantics do not change, and neither do the
Manifest format, Native ABI, Step protocol or Recording format.
`manifest.py` rejects an input Latency or a replay priority for which the
profile predicts no trajectory, before the Run. An arrival-time build of the
same sources is the negative control: it must fail the `delay` comparison.
