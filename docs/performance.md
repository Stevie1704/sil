# Routing performance

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## Large-Message routing baseline

The current data path copies a payload once from the publisher into the kernel.
It copies it again into every subscriber's queue.
[docs/bench/large-message-routing-baseline.md](bench/large-message-routing-baseline.md)
measures what that costs today, before the project commits to a leased buffer
pool. The matrix varies payload size, subscriber fan-out, recording on or off,
native against process participants, inline against shared-memory transport,
and burst delivery.

```sh
make bench                     # regenerate docs/bench/routing-baseline.{json,md}
```

Copy counts come from `sil-run-instrumented`, the same kernel sources compiled
with `SIL_COPY_COUNTERS`. Wall-clock comes from the production `sil-run`, which
carries no instrumentation. Keeping the two apart lets a run report copies as
counts instead of inferring them from timing. These counters are the whole
counter surface (#63): there is no metrics artifact and no metrics flag on
`sil-run`, because a run reproduces exactly, so re-running it instrumented
answers the question for the price of one run — an inertness the determinism
suite asserts per fixture, as the same exit code and the same recording bytes
under both runners. The report states the run's exit code and keeps its
repeatable counters (`deterministic`) apart from its wall-clock and RSS values
(`observational`).

`sil-run --no-recording` runs a manifest and writes no recording. That
separates the cost of routing to subscribers from the cost of recording I/O.
