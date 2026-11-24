# Scheduler verification

[Documentation index](README.md) · [Architecture](architecture.md)

Two kinds of evidence check the order in which the kernel runs Tasks and
Steps ([#404](https://github.com/Stevie1704/sil/issues/404)):

- **Generated trace regressions** run real Manifests through `sil-run` and
  compare the complete ordered Activation trace with an independent oracle.
  They run in `make test` and in the ordinary PR job. They need no Lean.
- **A Lean 4 model and its proofs** in
  [`verification/scheduler/`](../verification/scheduler/). The proofs show
  that a small scheduler model implements the scheduling contract for all
  valid inputs. A separate PR job checks the proofs and compares the model with
  `sil-run` on the same cases.

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

## The Lean model and its proofs

The Lake project pins its toolchain in `verification/scheduler/lean-toolchain`.

| Module | Content |
| --- | --- |
| `Scheduler/Spec.lean` | The declarative contract `IsSchedule`: a strictly ordered trace that holds exactly the due Activations. `IsSchedule.unique` shows that at most one trace satisfies it. `IsSchedule.count_due` shows that each due Activation occurs exactly once. |
| `Scheduler/Time.lean` | `checkedAdd`, the model of `virtual_time_after`. `checkedAdd_eq_some`: a representable sum is the exact mathematical sum. `checkedAdd_eq_none`: an unrepresentable sum gives nothing. |
| `Scheduler/Model.lean` | The executable model. It selects the earliest due Slot, orders the due entries of that Slot, and advances each entry with `checkedAdd`. An entry retires when its next time is unrepresentable or at or after the duration. |
| `Scheduler/Proof.lean` | `model_isSchedule` and `model_eq_schedule`: for every valid input, the complete ordered trace of the model is the declarative schedule. `Task.advance_unrepresentable`: an unrepresentable next time retires the entry. |
| `Main.lean` | `scheduler-model`, which runs the model on one input file. |

The theorems have no bound on the number of entries or on the magnitude of
times. They do not depend on the limits of the generated corpus.

The model reads and writes decimal text, which keeps all values up to
`UINT64_MAX` exact. `tests/test_scheduler_lean.py` sends each case of the trace
corpus to the model and compares the result with the oracle and with `sil-run`.
It uses the same fixtures and the same trace reader. If the executable is not
built, the test skips.

### Run the checks locally

Install [elan](https://github.com/leanprover/elan). It installs the pinned
toolchain on first use. Then, from the repository root:

```sh
cd verification/scheduler
lake build                  # proofs and the scheduler-model executable
lake check                  # Linux only: kernel replay, standard axioms only
cd ../..
SIL_REQUIRE_LEAN_MODEL=1 .venv/bin/python -m pytest tests/test_scheduler_lean.py
```

`lake check` builds the default targets, replays them through the Lean kernel,
and fails if a proof is unfinished or uses an axiom other than the standard
ones. It runs the build in a `bubblewrap` sandbox, which is available only on
Linux. On macOS, `lake build` fails on an error, but a `sorry` gives only a
warning. To see the axioms of a theorem, use
`lake env lean` on a file that contains
`import Scheduler` and `#print axioms Scheduler.model_isSchedule`.

The `scheduler-proof` job in `.github/workflows/ci.yml` runs `lake check` and
the comparison. During the pilot, the job is not a required check.

## Evidence boundary

- Lean proves properties of the scheduler **model**, for all inputs that the
  theorem covers: positive periods and distinct registration indices.
- The registration-index rules (name-sorted setup, declared Task order) are
  inputs to the model. The trace regressions test them against `sil-run`.
  The Lean proofs do not.
- The regressions show that `sil-run` agrees with the contract on the cases
  that they execute. They do not prove that the C++ scheduler is correct for
  other inputs.
- Each of these changes to `kernel/src/engine.cpp` fails cases: a reversed
  registration-index tie-break, and an Activation allowed at the duration.
- Replay ordering, routing and delivery, failure paths and Participant
  behavior are not in the scope of the contract.
