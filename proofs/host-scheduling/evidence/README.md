# Retained #362 scheduler-evidence qualification

Decision: **defer production adoption**. Direct host scheduler events explain
the named fixture's running, runnable and sleeping intervals. A supported
collector still needs an adopter workload and a separate reviewed state and
coverage contract. No production collector or existing v1 schema changes ship.

The [successful final Linux qualification](https://github.com/Stevie1704/sil/actions/runs/41791048279)
ran source `c3d9bc331a57488c5f9a09b70bbfa5044df6353f` on x86-64, kernel
`7.0.0-1017-azure`, four CPUs, glibc `2.39-0ubuntu8.9`, GCC
`13.3.0-6ubuntu2~24.04.1` and perf `7.0.14`. The archive retains exact package
inventories, source revision/hashes, workload/runner/tool binary digests,
workload build ID, event formats and container image identity. Existing host
policy stayed at `perf_event_paranoid=4` before and after the experiment.

| Environment | Result |
| --- | --- |
| Native UID 1001 | Unavailable: tracefs scheduler events were not readable |
| Native sudo-root, existing tracefs | 20 fresh Runs; phase, integrity and functional checks passed |
| Default root container/private PID namespace | Unavailable: tracefs/debugfs not exposed |
| Root container with PERFMON, unconfined seccomp, host PID namespace and read-only existing tracefs | 20 fresh Runs; checks passed |

Each unavailable attempt also retains one successful ordinary baseline.
No measured Run starts after a failed capture preflight. No remediation is
applied to a failed environment: the configured container is a separately
named experiment. Its initial PID namespace matches the native identity;
the default container's namespace differs. Default-container support is not
claimed. There are 42 executed Runs and 26 capture attempts in the final set.

## Independent state validation

Two Process participants share Virtual times 0, 1,000,000 and 2,000,000 ns.
Each Step marks fixed CPU work, 20 ms sleep, a child-to-parent synchronized
pipe wait and CPU work competing with a same-CPU child. A background thread
and exec helper provide independent worker/lifetime observations. The
[fixture and reducer](../README.md) describe exact semantics and limits.

Every normal measured Run retains six completed actual Process Step intervals,
all six expected deterministic Messages and two distinct main-TID lifetimes.
Three fresh repetitions per mode give 18 Steps per available environment.
Direct-syscall markers fit the actual runner intervals, all six scheduler
events prove CLOCK_MONOTONIC in their retained perf attributes, and trace
forks agree with runner spawn records and PID-namespace identities.

The following are medians of 18 independently marked phase intervals per
environment, in **milliseconds**. They are observations of individual
intervals, not pooled or averaged Run percentiles. Runnable contention is an
observation, never a duration assertion.

| Environment / phase | Scheduled running | Runnable | Sleeping |
| --- | ---: | ---: | ---: |
| Native / CPU | 16.4634 | 0.0000 | 0.0000 |
| Native / sleep | 0.0233 | 0.0054 | 20.0545 |
| Native / pipe wait | 0.0305 | 0.0041 | 20.1440 |
| Native / contended CPU | 32.9283 | 32.9942 | 0.0000 |
| Container / CPU | 16.4643 | 0.0000 | 0.0000 |
| Container / sleep | 0.0227 | 0.0052 | 20.0547 |
| Container / pipe wait | 0.0258 | 0.0050 | 20.1349 |
| Container / contended CPU | 32.9209 | 32.6052 | 0.0000 |

Normal main-TID Step intervals reconcile without unknown time in these
captures. This does not establish universal completeness or exact algorithm
CPU time. Initial/final states outside closed transitions remain unknown.
Worker overlaps are separately identified window associations and never added
to the main-TID totals. Native libtraceevent pretty formatting and container
raw trace-field formatting decode consistently. No CPU samples are collected
and no wall-minus-sample estimate or lock-cause inference is made.

## Perturbation, growth and deadline limits

Three fresh ordinary/observed repetitions, with identical marker
instrumentation and a generous 10-second Response deadline:

| Environment | Ordinary Run median ms | Observed Run median ms | Normal raw perf median bytes |
| --- | ---: | ---: | ---: |
| Native root | 1112.51 | 1109.62 | 831,650 |
| Configured container | 1112.07 | 1109.67 | 806,345 |

Small lower medians are variation, not negative or zero overhead. No
correction, significance claim or cross-workload bound follows. Collector
setup is separate: observed start-to-enable-ack medians were 462.06 ms
native and 190.29 ms container. File growth samples, exact controls, tool
export costs, raw files and per-Step intervals remain available. Normal perf
rings are 1 MiB per CPU, 4 MiB aggregate on this host. Raw guards, hard file
caps, grace periods and export/input budgets are in the proof README.

All three ordinary and all three observed Runs at the separate 180 ms
Response deadline succeeded in the final set. This faster host observation
does not establish deadline-edge inertness. A separately pinned
[earlier successful qualification](https://github.com/Stevie1704/sil/actions/runs/41790256050),
source `9a9ea7f` (full identity in its archive), retained actual mixed outcomes:

| Separate earlier experiment | Ordinary outcome | Observed outcome |
| --- | --- | --- |
| Native edge repetition 0 | Run failure, exit 1 | Completed, exit 0 |
| Container edge repetition 2 | Completed, exit 0 | Run failure, exit 1 |

`deadline-edge-observations.tar.xz` keeps these four Runs with exact original
results/inventory, selected raw data/Recordings/diagnostics/markers, the
recorded proof sources, newly sealed subset inventories and curation source.
It is a separate experiment and revision, never pooled into final-run
comparisons. Those pairs show the limit of a timing-edge equality claim;
they do not attribute every difference to a constant collector overhead.

## Failure, loss and interruption

- Generous-deadline ordinary/observed successful Runs retain equal outcomes
  and Recording SHA-256 values, including collector interruption and guard
  stops. The deliberate Participant failure also retains equal Recordings
  and exit 1. Every normal Message matches the independent fixed result.
- The intentional hard kill exits the runner with SIGKILL. Its buffered
  Activation observations are absent, not completed by fixture end markers.
  Three earlier fixture-closed Steps report `interval_absent`; the unmatched
  `slow` index-1 begin reports `unclosed`. No completed Step statistic or
  duration is invented. Clock/namespace joins remain unverified there.
- Interrupting perf during the first sleep leaves later lifetimes and
  transitions unknown while the independent Run completes with equal output.
- The 64 KiB raw guard requests stop at the next 10 ms poll. Final files are
  383,050 bytes native and 345,017 bytes container; the threshold is not a
  hard quota. Missing startup/descendant mapping remains unverified.
- Stopping the perf reader briefly with one-page rings produces genuine
  `PERF_RECORD_LOST` and export warnings in both environments: four native
  and five container lost chunks, with separately reported sample-loss
  percentages. Loss invalidates all state-duration classification when exact
  gap boundaries are unknown. Local missing-switch/wakeup reasons remain
  separate. Loss counts are not combined into invented nanoseconds.
- Deliberately halving finalized raw files makes export/clock/mapping
  unavailable or truncated; no complete state classification is fabricated.
  This is a saved-input truncation experiment, not an ENOSPC qualification.

## Replay without Linux or perf

`archive.sha256` binds both archives. The final portable archive is
4,697,064 bytes for 155,623,386 payload bytes; the separate deadline-edge
archive is 637,668 bytes. Raw perf files and full event/statistics exports
are retained, as are the exact measured proof sources. Bytecode caches are
the only final-artifact omission. The full Actions artifacts also remain
available at the linked runs for their 30-day retention.

```sh
cd proofs/host-scheduling/evidence
shasum -a 256 -c archive.sha256
replay_dir=$(mktemp -d)
tar -xJf portable-evidence.tar.xz -C "$replay_dir"
for environment in "$replay_dir"/scheduler-evidence/*/; do
  if [ -f "$environment/results.json" ]; then
    python3 "$replay_dir/scheduler-evidence/proof-sources/validate.py" "$environment"
  fi
done
edge_dir=$(mktemp -d)
tar -xJf deadline-edge-observations.tar.xz -C "$edge_dir"
for environment in native-root-existing container-perfmon-hostpid; do
  python3 "$edge_dir/proof-sources/validate.py" "$edge_dir/$environment"
done
```

Final validation reports 20 executed Runs/12 capture attempts for each
available environment and one executed baseline/one unavailable attempt for
each default environment. File integrity is checked before decoding; the
reducer reruns from saved text, markers and actual Activation observations.
Independent report fixtures cover half-open boundaries, PID reuse, equal
Virtual times, missing transitions, preemption, tracked/unrelated exec,
child associations, loss/truncation and failed/unclosed observations. A test
also unpacks and replays the final archive offline.

The [production decision and reopening criteria](../../../docs/profiling-investigation-decisions.md#schedulerblocking-evidence--362-defer-production-after-qualification)
require a named adopter question, permitted deployment, capture/perturbation
budgets and a separate reviewed state/coverage schema before implementation
tickets. Ordinary `sil-run`, scheduling, functional verdicts, Manifest/hash,
Native ABI, Step protocol and canonical Recordings are unchanged.
