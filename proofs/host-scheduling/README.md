# Host scheduling evidence for slow Process Steps (#362)

This is a finite experiment, with a **defer** decision for a production
collector. It investigates a Step containing fixed CPU work, explicit sleep,
a synchronized pipe wait, and CPU work competing with a child pinned to the
same CPU. It adds no supported command, schema, Run-control dependency,
ordinary-run measurement hook, Manifest field, ABI or Step-protocol change.

`workload.c` is compiled with `-O2 -g -fno-omit-frame-pointer` and called by
`participant.py` through the public Process participant interface. Two
Participants, `peer` and `slow`, share Virtual times 0, 1,000,000 and 2,000,000
ns. Every successful Step publishes its index and a fixed integer result.
Neither scheduler events, host clocks, wait durations nor the background
thread influence those Messages. No Clock shim is requested. A background
thread deliberately runs outside and during Steps; child processes exercise
pipe wakeups, CPU contention and exec/exit. The exec helper is a separately
identified executable built from the same source.

Independent phase markers use a direct `SYS_clock_gettime(CLOCK_MONOTONIC)`
call and record PID, TID, parent, PID-namespace identity and actual Virtual
time/index. They are written to a separate append-only file in every mode,
including the ordinary baseline. These markers identify fixture intent; they
never assign a scheduler state. CPU publications use a fixed linear
recurrence modulo 2^64. Independent affine-composition arithmetic gives the
expected result `3776395645756024896`; the validator checks all six expected
Messages, or the three Messages preceding the deliberate failure.

## Reproduce on Linux x86-64/glibc

Install the Python package, a compiler, CMake, OpenSSL development files,
binutils and the perf package matching the running kernel. The proof never
installs packages or changes permissions. Use fresh work/evidence directories:

```sh
.venv/bin/python proofs/host-scheduling/qualify.py build "$PWD/build/scheduler-work"
initial_ns=$(sudo stat -Lc '%d:%i' /proc/1/ns/pid)
.venv/bin/python proofs/host-scheduling/qualify.py run "$PWD/build/scheduler-work" \
  --out "$PWD/build/scheduler-evidence" --environment native-unprivileged \
  --initial-pid-namespace "$initial_ns"
```

The namespace argument must come from the **native host**, before entering a
container. Reading a container's own `/proc/1` does not prove the initial
namespace used by scheduler tracepoint payloads. Root capture is a distinct
invocation and privilege context, never an unprivileged qualification. If
event access or startup acknowledgment fails, the attempt is unavailable and
does not start a measured Run. Its ordinary baseline, exact command, error,
policy and identities remain evidence. Nothing remounts tracefs or changes a
sysctl in response to a failure.

The manually dispatched workflow retains native unprivileged, native sudo-root and ordinary
container attempts before an explicitly configured container experiment.
That experiment uses `--pid=host --cap-add PERFMON
--security-opt seccomp=unconfined` and a read-only bind of existing native
`/sys/kernel/tracing`. It does not qualify a default container or a private
PID namespace. The image installs the native host's exact perf package version;
image ID/digests, package inventories, compiler/glibc/perf versions, source
revision/hashes, workload/runner/tool digests and workload build ID are retained.
The original and final `perf_event_paranoid` values must agree. The experiment
does not grant capabilities to the host or make host mounts.

## Direct events and conservative interpretation

The perf path records system-wide `sched_switch`, `sched_wakeup`,
`sched_wakeup_new`, `sched_process_fork`, `sched_process_exec` and
`sched_process_exit`, with `--clockid mono`. The retained `perf evlist -v`
attributes must prove `use_clockid=1, clockid=1` for all six events.
Independent phase markers must fit actual diagnostics-runner intervals;
spawn observations, tracepoint parent/child PIDs and namespace identities must
agree. A clock label or a numerically equal PID alone is insufficient.

The reducer uses only directly closed, consistent transitions:

| Observation | Interpretation after that event |
| --- | --- |
| Switch in | Running (scheduled on a host CPU) |
| Switch out with `R` or `R+` | Runnable, including preemption |
| Switch out with `S`, `D` or `I` | Sleeping; no particular IPC/lock cause |
| Successful wakeup or new-task wakeup | Runnable, unless already running |
| Stopped/traced/exiting/unsupported state | Unknown for this four-state view |
| Missing expected switch/wakeup | Previous uncertain interval becomes unknown |

Running intervals do not subtract interrupts or hypervisor steal time and are
not exact function CPU durations. Fork starts a distinct TID lifetime. Exit closes it; later reuse needs a new
fork and cannot inherit the old state. Leader exec preserves the lifetime;
nonleader exec/TID replacement is explicitly unqualified. Descendant scope
expires on exit or replacement; an unrelated fork cannot inherit membership
by reusing a descendant or runner TID. Initial state and
the tail after the last transition stay unknown. The proof follows all
captured descendants of the runner, with separately marked thread/child
roles. It makes no general guarantee about detached or namespace-changing
descendants. Synthetic report fixtures independently exercise PID reuse,
missing wakeups, preemption, equal Virtual times and half-open boundaries.

Actual Process Step intervals come from the existing diagnostics observations,
using `[begin_ns,end_ns)`. An auxiliary thread or child's overlap is a separate
`worker_window_association`, never exclusive Step work or additional main-TID
time. This label describes host threads/children, never a Process participant.
Fixture phases are separate windows. They validate observed states but
cannot replace a missing runner interval. Failed intervals retain their own
outcome and are excluded from completed-Step counts. A hard-killed runner can
leave only a fixture begin marker; its actual interval and duration stay absent.

The named sleep and synchronized wait are independent fixture ground truth.
Seeing `S` in another Participant proves neither a pipe wait nor a lock cause.
This proof does not collect sampled CPU at all and never subtracts samples
from elapsed time. No guest RTOS, target ECU, WCET or exclusive algorithm-time
claim follows.

## Bounds, loss and cleanup

Each available environment has 20 fresh cases: three ordinary/observed
success pairs, deliberate failure and hard-kill pairs, plus collector
interruption, a 64 KiB guard request, a one-page-ring loss attempt, and
deliberately truncated saved data. Three additional ordinary/observed pairs
use an explicitly selected 180 ms Response deadline near the initially
observed Step cost. Their outcomes are observations, never an equality or
timing assertion. Every capture has one independent perf
session and one Run. There is no live scheduler wait or collector hook in the
kernel. The supervisor enables perf and waits for acknowledgment **before**
starting the Run. Ack/exit observations are not exact collection boundaries.

Normal rings are 1 MiB per CPU (recorded host CPU count determines aggregate
ring memory). A 10 ms guard polls a 32 MiB raw limit; a hard inherited
`RLIMIT_FSIZE` of 64 MiB caps each perf/export file even between polls.
The guard threshold is not a hard quota. Whole-Run supervision is 30 seconds,
perf's safety workload is 35 seconds, tool exports have a 120-second limit,
and collector/runner termination has a 5-second grace before SIGKILL.
Activation observations are independently capped at 1 MiB. Fixture markers
and Messages are bounded by the fixed three Steps and finite threads/children.
Each analysis input is capped at 256 MiB. There is no interactive data or DOM.

`PERF_RECORD_LOST`, loss statistics, export warnings/errors and unparsed
events remain distinct from an intentional collector stop. Without known
loss boundaries, any detected loss or truncation conservatively makes all
state durations unknown; the retained direct events are not discarded.
Even clean output does not certify absence of undetectable missed transitions.
Local transition inconsistencies invalidate their uncertain intervals.
An interrupted collector leaves initial/final coverage unknown rather than
extending the last known state to the end of a Step.

The Run is launched independently of perf and owns the existing Participant
cleanup path. Stopping or interrupting the collector does not signal the Run.
Failure and hard-kill fixtures are intentional and separately identified.
The hard-kill fixture first joins its background thread, kills its parent
runner, then exits; its peer sees protocol EOF and closes its thread.
No cleanup behavior is inferred from a successful collector exit.

Three fresh repetitions retain elapsed Run times, collector setup/control
observations and raw-file growth. They include marker instrumentation in all
modes and supply no general overhead correction or significance claim.
The normal 10-second Response deadline is deliberately generous. Equality
there gives no timing-edge guarantee: instrumentation, scheduling
and disk contention can move a workload across a Response deadline. No noisy
timing threshold is a CI correctness gate.

## Evidence, decision and source semantics

Qualification runs only on explicit dispatch for a named investigation:

```sh
gh workflow run proof-host-scheduling.yml --ref proof/362-host-scheduling
```

PR updates run the ordinary repository checks and do not start privileged
scheduler collection. The committed `Dockerfile` defines the proof image;
the workflow records its base and derived image identities. perf and Ubuntu
notices are documented in [THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).

The [retained evidence](evidence/README.md) supplies measurements, environment
dispositions and replay commands. `validate.py EVIDENCE` verifies saved file
digests, repeats decoding/state reduction without perf, checks the fixture
and outcome/Recording equality, and distinguishes unavailable attempts.

To inspect one saved capture with the current decoder without replacing its
reports, use `decode.py CAPTURE_DIR FRESH_OUTPUT.json`. This joins actual
observations, markers, event attributes and scoped scheduler events. The
`report.py INPUT.json FRESH_OUTPUT.json` reducer accepts normalized events;
there is no separate test-only perf-text entry point.

Begin markers are points inside `[begin,end)`. Fixture end markers denote
exclusive interval boundaries and may equal the Activation end. Reports
retain the ordered state intervals, including explicit initial/final unknown
regions; validation checks adjacency and bounds rather than using a seeded
unknown-duration total as evidence of a partition. The CPU, sleep, pipe-wait
and contended phases each require their independently expected state to have
been observed. There is no minimum-duration threshold for runnable delay.
Collector interruption and size-limit reasons remain explicit even when
later PID/namespace evidence is absent.

After downloading the Actions artifact into a fresh directory, retain it with
the same tool used for the committed evidence:

```sh
gh run download RUN_ID --name host-scheduling-evidence --dir build/scheduler-artifact
python3 proofs/host-scheduling/retain.py build/scheduler-artifact \
  build/scheduler-retained --run-url https://github.com/Stevie1704/sil/actions/runs/RUN_ID
```

This writes `portable-evidence.tar.xz` and `archive.sha256` in the fresh output
directory. Historical archives keep their original sources and validator;
they are not silently regenerated with a newer decoder.

The [decision record](../../docs/profiling-investigation-decisions.md) names
the production gaps and reopening criteria. **Defer** requires no production
state schema or implementation ticket; a future **build** decision must
first define its own reviewed versioned state/coverage contract and tickets.
Existing v1 profile/correlation meanings are untouched.

The experiment reuses [#330's measured permission/container/stack findings](../heaptrack-shim/availability-notes.md).
Its successful malloc probe did not establish scheduler coverage; no new
eBPF candidate is necessary for this perf-path investigation. Its stack
resolution limits remain relevant to any future attempt to infer a cause.

Primary semantic references (inspection only; no upstream source is copied):

- Linux [scheduler event definitions](https://github.com/torvalds/linux/blob/v7.0/include/trace/events/sched.h):
  switch-out states, preemption and successful/new-task wakeups. The exact
  running kernel's tracefs event formats are retained too.
- perf [record options](https://github.com/torvalds/linux/blob/v7.0/tools/perf/Documentation/perf-record.txt)
  and [script fields](https://github.com/torvalds/linux/blob/v7.0/tools/perf/Documentation/perf-script.txt).
- perf [control implementation](https://github.com/torvalds/linux/blob/v7.0/tools/perf/util/evlist.c):
  serialized commands and NUL-terminated acknowledgment. No command time is
  treated as an exact host scheduling transition.
