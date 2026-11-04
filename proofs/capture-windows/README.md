# Late CPU capture-window experiment (#361)

This proof compares whole-Run CPU collection/post-filtering with explicit
Virtual-time `[610000000,611000000)` control on one fresh Run and one perf
collector. It adds no supported CLI, scheduler wait, ordinary-run hook,
Manifest field, ABI change, trigger language, ring buffer or heap window.

`workload.c`, compiled with `-O2 -g -fno-omit-frame-pointer`, performs fixed
integer work on 620 Activations per Task. Task `late` enters the known
`late_hot_function` only at index 610; Native Task `peer` shares its Virtual
times. `process.py` runs the same compiled work through the Step protocol.
Independent workload entry/exit markers remain separate from diagnostics
notices, completed Activation observations and sampled stacks. Published
Task/index values do not depend on any host observation.

The proof-only CMake overlay copies and patches just the diagnostics
translation unit into the build directory. Constructor/exit notifications use
a nonblocking Unix datagram send, with explicit drops and a finite receiver
budget. The ordinary target continues to compile the repository's untouched
sources. A supervisor serializes named perf commands and records its receipt,
write and acknowledgment times. No Activation waits for a collector.

The existing Activation writer (`record_activation`) emits its record only
after completion, through a buffered regular file. It cannot start collection
for that same Activation. Tailing or polling it would add an uncontrolled
completion/buffering delay; using a requested Virtual-time value as a host timer
would conflate clocks. The proof therefore adds just nonblocking begin/return
notifications to the diagnostics build. Its notifier also sends one shutdown
notice during normal unwinding, before writing its loss footer; SIGKILL cannot
send it. No per-Task string lookup or collector acknowledgment enters scheduling.

On Linux x86-64/glibc with perf, a compiler, CMake and the Python package:

```sh
python proofs/capture-windows/qualify.py build "$PWD/build/window-work"
python proofs/capture-windows/qualify.py run "$PWD/build/window-work" \
  --out "$PWD/build/window-evidence" --environment native
```

Both directories must be fresh. Commands, package versions, host policy,
binary hashes/build IDs, raw perf data, metadata, samples, controls, file
growth, workload markers, diagnostics and Recordings are retained. Three
fresh repetitions compare ordinary, diagnostics, observer, whole and bounded
modes. The proof adds a 60-second supervisor guard and explicitly selects a
10-second Process response deadline; these are experiment policies, not
production defaults. Times and file sizes are
observations, never timing correctness thresholds or an overhead correction.
The production-default 1 GiB capture limit, 100 ms guard polling and 8 MiB storage
reserve remain explicit; reserve/size stops preserve Run execution. The size
case explicitly lowers the limit to 64 KiB. Named CPU disable intentionally
differs from production's all-event disable to retain the dummy tracking event.
Sampling is 997 Hz here, versus production's 999 Hz. Every result records these
policies; this proof does not claim identical collector behavior or overhead. Export has a separate
256 MiB/180-second budget. There is no automatic permission change.

The manually dispatched workflow records native/container availability before changing host
policy. It is not a PR gate. The capability experiment explicitly uses CAP_PERFMON and an
unconfined seccomp profile. Native sampling explicitly sets paranoid 2.
A separate 16 MiB tmpfs tests active collection crossing the existing 8 MiB
reserve. A second case deliberately consumes the remaining space between
guard polls to expose actual collector ENOSPC behavior. Run outputs live on
the main filesystem. This injected competing writer can defeat a reserve;
its measured Run/collector outcomes remain separate.

Cases include equal-time Tasks, range and exact-Task edges, empty/unreached
selection, delayed enable and a host stop during ongoing work, failed and
SIGKILL-unclosed work, socket backpressure, capture-limit overshoot and reserve
stops. Intentional CPU disable, observer loss, unknown control transitions and
perf loss/truncation must remain distinct. Acknowledgment is neither the
first/last sample nor a drain guarantee; zero samples do not prove complete
coverage. See [source findings](../../docs/research/perf-capture-window-control.md).

The [retained evidence and decision](evidence/README.md) record Linux native
and container measurements. Long-window volume reduction is positive;
production adoption is **deferred** because asynchronous boundaries need a
reviewed coverage contract and actual ENOSPC can terminate the perf-owned Run.
A positive feasibility result alone cannot ship a collector.

`validate.py EVIDENCE` replays retained inputs and writes `analysis.json`
without perf or workload binaries. It checks the named
experiment's counts, identity/outcome equality, setup metadata and explicit
loss/guard cases, including SIGKILL and ENOSPC outcomes. `--report-only`
reports partial fixtures without asserting that the whole experiment ran.
A permission-denied environment remains unavailable.
The analysis aggregates counts by independently marked host intervals; these
are time-window associations across sampled processes, not exclusive Task or
Process CPU accounts. A workload helper return does not imply a completed
Activation. First/last samples do not establish continuously observed work.

Independent report-boundary tests cover collector/Run outcome separation,
invalid acknowledgment order, actual Virtual time, disabled request regions,
early termination and rejection of false SIGKILL/ENOSPC results:

```sh
SIL_SKIP_BUILD=1 .venv/bin/python -m pytest tests/test_capture_windows_proof.py
```

For a task-less window, the first begin notice at or beyond `to_ns` requests
disable. A failed Run may never reach that Virtual time. Shutdown then requests
disable separately; it does not pretend that the requested end occurred. The
report records `not_reached_run_failed`/`not_reached_run_killed`, the terminal
notice (if any), control writes/acks and observed collector exit. A late or
missing shutdown acknowledgment remains unknown. CPU may be collected through
failure/unwinding until control or collector exit; no host deadline is inferred
from the requested Virtual-time range.

`disabled_regions` records deliberate disable request intervals with their
command/ack/collector-exit observations and counts any samples in those
transition intervals. Effective boundaries remain unknown; loss/truncation
are separate reasons. Workload markers carry actual Virtual time, and the
real-run validator checks that index 611 is retained outside the selected
region and that its begin notice triggers the normal window end.
