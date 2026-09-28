# Documentation

Start with the [repository overview and quick start](../README.md).
Commands in the guides run from the repository root unless stated otherwise.

## User guides

| Task | Guide |
| --- | --- |
| Run the ACC example or install a staged release | [Examples and installation](getting-started.md) |
| Configure Run guards, the Clock shim, and Channel Transport | [Running simulations](running.md) |
| Import and inspect an FMU, bind variables, or connect network terminals | [FMI importer](fmi.md) |
| Convert CSV data and replay it into an FMU | [Recorded input and FMU replay](recorded-input.md) |
| Couple FMUs through Channels or replace one with replay | [Coupling and substituting FMUs](fmu-coupling.md) |
| Bring an existing C library into a Run | [Shared-library participants](library.md) |
| Select a replay window, warm up a target, or replay long Recordings | [Replay](replay.md) |
| Compare outputs against a reference trajectory | [Comparisons](comparison.md) |
| Seal and run regression cases and matrices | [Regression bundles](regression-bundles.md) |
| Run one Manifest in a Linux container | [Container deployment](container.md) |
| Find acceptance evidence and maintained models | [Acceptance proofs](acceptance.md) |
| Understand routing measurements and memory costs | [Performance](performance.md), [routing baseline](bench/routing-baseline.md), [large-message baseline](bench/large-message-routing-baseline.md) |

## Architecture and contracts

- [Architecture and repository map](architecture.md) describes the current
  module responsibilities and build ownership.
- [CONTEXT.md](../CONTEXT.md) defines the domain vocabulary used throughout the
  code and documentation.
- [Step protocol](step-protocol.md) is the normative Process participant
  protocol. [Public headers](../include/sil/) define the native and shared-memory
  interfaces. [SUPPORT.md](../SUPPORT.md) defines compatibility and support.
- [DESIGN.md](../DESIGN.md) records the original architecture and its historical
  amendments. Its original milestone and deferred-work statements must be read
  together with the later decisions.
- [docs/adr/](adr/) contains accepted architectural decisions, including
  [FMU groups](adr/0001-connected-fmus-in-one-process-participant.md),
  [terminal replay timing](adr/0002-a-replayed-terminal-lands-on-its-own-instant.md),
  and [maintained CAN models](adr/0003-maintained-can-model-at-the-edge.md).
  Record new architectural decisions here; consult the applicable ADR and its
  status when an older design statement differs.
- [docs/decisions/](decisions/) retains the historical
  [consumer capability gate](decisions/118-consumer-capability-gate.md).
  It is the evidence required for expanding kernel scope, rather than another
  location for new ADRs.
- [Proposals](proposals/) and [research](research/) describe prospective work
  and investigation; they do not establish implemented or supported behavior.

## Contributing and releasing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for contribution terms and required
checks, [releasing](releasing.md) for the release process, and
[release notes](releases/) for published changes. Agent-specific repository
instructions live in [AGENTS.md](../AGENTS.md) and [docs/agents/](agents/).
