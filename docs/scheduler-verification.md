# Scheduler verification

[Documentation index](README.md) · [Architecture](architecture.md)

Generated trace regressions check the order in which the kernel runs Tasks and
Steps ([#404](https://github.com/Stevie1704/sil/issues/404)). They run real
Manifests through `sil-run` and compare the complete ordered Activation trace
with an independent oracle. They run in `make test` and in the ordinary PR
job.

## The scheduling contract

The contract covers successful Runs with periodic Native Tasks and Process
Steps, and no Replay participants. Here, an *entry* is one Task or one
Process Step: the unit that the scheduler activates. Each entry has a stable
identity, a positive period, an offset (zero for a Process Step), a signed
priority and a distinct registration index. The Run duration and all times
are `uint64_t` values.

- An entry is due exactly once at every time `offset + k × period`, for an
  integer `k ≥ 0`, with a result in `[0, duration_ns)`. The contract is
  calculated over mathematical integers. An Activation at the duration is
  excluded. A next time that `uint64_t` cannot represent never wraps into an
  earlier Slot and does not affect another entry.
- The trace is ordered by increasing Virtual time, then increasing priority,
  then increasing registration index. All Activations in one Slot have the
  same Virtual time.
- Registration indices follow the name-sorted Participant setup, whatever the
  authoring order. In one Native participant, they follow the declared order
  of its Tasks.

## Generated trace regressions

`tests/test_scheduler_trace.py` runs each case through `sil-run`.
`tests/scheduler_trace.py` holds the cases, the oracle and the trace reader.

- Each Activation publishes one marker. Native participants use the
  `trace_tasks` fixture, which registers the Tasks in its config. Process
  participants use `tests/participants/trace_step.py`. Each Participant
  publishes on its own Channel. A marker holds the Virtual time that the
  kernel passed to the Task and the index of the Task in its Participant.
- The interceptor drops every Publish at or after the Run duration, so a
  marker cannot show an Activation there. Both fixtures therefore fail the Run
  if the kernel activates them at or after the duration.
- The Manifest lists the Participants in authoring order. `Manifest.write`
  sorts the keys, so the test writes the file itself. A case that authors the
  Participants out of name order then tests the name-sorted setup.
- The reader reads the markers in stored Publish order, with no sorting or
  deduplication. Thus a reversed tie, a duplicate, or a missing or extra
  Activation changes the observed trace.
- The oracle enumerates the times of each entry over Python integers and sorts
  the result by `(time, priority, registration index)`. It does not share code
  with slot selection, the overflow guard or the Recording output of the
  runner.
- The corpus has fixed edge cases and 48 generated cases from a fixed seed
  (`CORPUS_SEED`). Each case has its own seed, so you can run it alone.
  Each period is at least a fixed fraction of the duration, so the number of
  Activations is small whatever the magnitude of the times. Each Run has a time
  limit.

A failure names the case, the first differing entry (or an early or extra end
of the trace), the input schedule, and the command that runs the case again:

```sh
.venv/bin/python -m pytest tests/test_scheduler_trace.py -k 'gen-004'
```

## Evidence boundary

- The regressions show that `sil-run` agrees with the contract on the cases
  that they execute. They do not prove that the C++ scheduler is correct for
  other inputs.
- Each of these changes to `kernel/src/engine.cpp` fails cases: a reversed
  registration-index tie-break, and an Activation allowed at the duration.
- Replay ordering, routing and delivery, failure paths and Participant
  behavior are not in the scope of the contract.
