# 3. Maintain the standalone CAN bus model at the edge

Issue #152 authorizes expanding the earlier core-only product scope with a
maintained C++20 CAN Bus Simulation FMU under `models/can/`. CAN semantics belong
in a standalone model core, with a thin FMI 3.0 C adapter; FMI coordination stays
in the Importer as ADR 0001 defines it, and replay retains ADR 0002's event-time
and zero-Latency contract. A first-party model brings maintenance and profile
qualification obligations, but permits deterministic model behavior without
making the kernel a CAN implementation. The earlier upstream proof remains
independent evidence. This is an authorized model product addition, not evidence
of a missing kernel feature under #118, and changes no kernel/transport,
Manifest/hash, Step, Arena, Native participant ABI, successful Recording or
exit-code contract. The initial fixed-delay smoke profile makes no transmission
timing or contention claim.
