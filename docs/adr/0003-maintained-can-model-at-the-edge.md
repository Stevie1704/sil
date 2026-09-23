# 3. Maintain the standalone CAN bus model at the edge

Date: 2026-09-23

Status: accepted

Relates to [issue #152](https://github.com/Stevie1704/sil/issues/152).

## Context

The CAN milestone authorizes expanding the earlier core-only product scope
with a maintained CAN Bus Simulation FMU. Maintaining the model adds profile
qualification and compatibility obligations, but permits deterministic CAN
behavior without making the kernel a CAN implementation.

## Decision

Maintain the standalone C++20 model under `models/can/`, with CAN semantics in
a model core and a thin FMI 3.0 C adapter. FMI coordination stays in the Importer
as [ADR 0001](0001-connected-fmus-in-one-process-participant.md) defines it;
replay retains [ADR 0002](0002-a-replayed-terminal-lands-on-its-own-instant.md)'s
event-time and zero-Latency contract. The earlier upstream proof remains
independent evidence.

## Consequences

This is an authorized model product addition, not evidence of a missing kernel
feature under [issue #118](https://github.com/Stevie1704/sil/issues/118). It changes
no kernel/transport, Manifest/hash, Step, Arena, Native participant ABI,
successful Recording or exit-code contract. The initial fixed-delay smoke
profile made no transmission timing or contention claim; issue #153 replaced it
with the documented wire-timing model in `models/can/README.md` using only the
existing countdown-Clock Importer capability. Issue #154 added bounded
contention in the standalone model and same-instant bus-input batching at the
Importer edge, while preserving this placement decision. The maintained FMU
must publish its supported profile and qualify both its protocol behavior and
its packaging independently of the kernel.
