# Source debugging at a selected Activation (#321)

Phase 2 feasibility investigation under #313. All scripts are proof-owned;
there is no supported debugger CLI, schema, ABI, protocol or kernel hook.
The workload is the unchanged ADAS reference C application in
`examples/adas-reference/`. Only its build and external debugger differ.

## Question and reproducible experiment

Can external GDB scripts explain the hazard controller's last two ramp Steps
in its Native, Process and FMU forms? The expected trajectory is authored in
`maneuvers/hazard.expected.csv`, independently of the implementation:

| Output sequence | Activation index | Activation Virtual time | Output Sample time | Previous / limited command (m/s²) |
| --- | --- | --- | --- | --- |
| 5 | 4 | 40,000,000 ns | 50 ms | −2 / −2.5 |
| 6 | 5 | 50,000,000 ns | 60 ms | −2.5 / −3 |

The index is zero-based per Task/Process Step, not a global breakpoint count.
`adas_ref_advance` increments `activations` and writes sequence = index + 1;
the output Sample time is the end of `[t, t + Period]`. Recording publication
timestamps and Activation observations carry the start, not that Sample time.
The proof requires the breakpoint hit count and `t_ns` to agree with the
actual `observations.jsonl` records. Two separate fresh Runs select by index
and by Virtual time. This fixture has one application call per Activation;
that relationship must be re-established for a different adapter/workload.

Real qualification runs in `.github/workflows/proof-source-debugging.yml` on
GitHub Actions `ubuntu-24.04`, Linux x86-64/glibc. macOS/arm64 can compile the
Python scripts and run existing workload tests, but supplies no GDB evidence.
The workflow pins GDB `15.0.50.20240403-0ubuntu1` and GCC 12
`12.4.0-2ubuntu1~24.04.1`. Missing pinned packages fail setup. No sysctl,
capability, seccomp or ptrace setting is changed. `attach.json` records
`kernel.yama.ptrace_scope` and the actual result of a sibling-process attach;
launch-under-GDB works without relying on sibling attach permission.

```sh
# On the workflow's Linux host, after installing its packages/Python deps:
bash proofs/source-debugging/run-proof.sh \
  "$PWD/build/source-debugging-evidence" "$PWD/build/source-debugging-build"
# Local workload regressions:
.venv/bin/python -m pytest tests/test_example_adas_reference.py tests/test_adas_process_adapter.py
# Offline evidence tests and validation (no GDB or executable loading):
.venv/bin/python -m pytest tests/test_source_debugging_proof.py
.venv/bin/python proofs/source-debugging/validate.py CAPTURE_DIR native
```

`-O2 -g -ffp-contract=off` is the main result. There is no `-O0` substitute,
source probe, noinline attribute, injected inferior call or variable mutation.
`build-commands.json` and copied sources state exactly what was compiled.
Every proof-owned runner, workload library and Clock shim has a separate
debug file. `identities.json` records binary/debug SHA-256 and build IDs,
`.gnu_debuglink`, and the existing profiler's matching result. The FMU archive
includes the matching debug file beside its optimized library, so GDB can
resolve it before the importer removes its extraction directory. The archive
is a proof build, not the released example archive. It does not modify a
Manifest to enable debugging.

## Stops, ownership and values

Each selected ADAS Activation retains entry/decision/return observations,
plus an earlier selection inspection:

1. **Entry** to `adas_ref_advance`, after the adapter has delivered inputs,
   before this call's algorithm/state update. Read `instance`, held radar and
   camera lists, held ego metadata, previous command, `activations`, `t_ns`.
2. **Selection and hazard inputs**, before their last use: stop after
   `select_lead` and inspect the selected ID and the fields of `lead`
   individually. A source line may precede the `closing_mps` initializer, so
   at most 32 `nexti` operations reach the known fixture's closing speed of
   25 m/s. This is an explicit, workload-specific stop condition, independently
   checked against the authored hazard fixture by the offline validator.
   A pre-initialization read is unavailable with reason
   `before_hazard_computation`. No variable or register is written.
3. **Decision**, at the optimized source location for `limited`: read `lead`,
   `selected`, `mode`, `target` and the old command where DWARF makes them
   available. Hazard is a function/decision, not a stored `hazard` variable;
   mode 1 and output target −3 establish its result for this fixture. Inline
   `limited` arguments may be unavailable. Source lines can move or combine
   under optimization; the actual PC's source location and stack are saved.
4. **Return** from the C API, before the caller publishes: read output memory,
   limited result, sequence and Sample time. This does not mean a Message has
   already been recorded; the resumed adapter still has to publish it.

`selection-stops.json` retains the earlier live `selected`, `lead.x_m`,
`lead.relative_vx_mps` and initialized `closing_mps`. These come from their
own DWARF locations, independently of returned output or held observations.
The aggregate `lead` and its other fields may still be optimized out.
`stops.json` retains raw GDB locals, stack frames, source/line, loaded-module
paths, inferior PID/PGID, TID, host stop time and expression-level values.
Unavailable/optimized-out values stay null with a reason; no later output or
hand calculation is relabelled as a historical local. The application does
not retain ego speed in `adas_ref_instance`: `ego_held` contains validity,
sequence and origin only. Reading the full ego observation would require a
separate stop in `adas_ref_receive_ego`; this proof makes no claim to recover
discarded speed from the advance frame. The unchanged source establishes
that limitation.

Native debugging launches the runner itself: its thread executes the Native
Task and an all-stop breakpoint pauses the entire runner, including all
Native participants. This fixture has only one Native controller. Process
and FMU debugging uses `sil-run-diagnostics --launch-wrapper hazard ...`:
the selected child execs GDB, which launches the real adapter as its inferior.
The runner remains a different process waiting for a Step response. GDB owns
the wrapper PID; the inferior has another PID. Do not join a profiler's
Process PID to the inferior by coincidence. The proof saves actual process
identities and checks they differ from the runner. The hook is diagnostics-only;
ordinary Runs have no wrapper. GDB output goes to its own log while inferior
stdin/stdout remain the Step protocol's pipes.
The pinned GDB uses its normal startup shell to interpret the quoting it
constructs for `--args`; disabling that shell after argument parsing split
the Process adapter's JSON argument in the qualification control. This proof
leaves startup-with-shell on, preserving the Manifest's actual argument vector.

The named ADAS FMU currently runs through the **single-FMU importer**. The
group importer requires FMI BUS terminals and a BUS profile; the ADAS FMU has
neither. `group.json` and its rejected Run retain that mismatch in the ticket's
premise. This experiment does not manufacture a grouped ADAS implementation.
A supplemental, runnable BUS group qualifies that shared boundary (ADR 0001):
two CAN node instances and a bus instance, using the existing stand-ins in
`tests/fixtures/fmi_clock.c` and `fmi_bus.c`, packaged by `tests/can_fixture.py`.
This tests the real group importer without adding BUS terminals to ADAS.
The group uses one Process participant with a 100 ms Period. Index 2 and
Virtual time 200 ms select the same inclusive Step, which advances the group
toward its first 300 ms transmission. GDB stops in each instance's
`fmi3DoStep`, recording three distinct instance pointers and loaded module
paths in one inferior PID, with the runner and wrapper PIDs recorded separately.
Every inferior thread is stopped, the inspected state stays unchanged across
a 50 ms host pause, and the inferior has no child FMU processes. Resuming each
capture must reproduce the two ordinary group Recordings exactly. This is
group-process boundary evidence, not a new CAN semantics qualification or
an implementation of grouped ADAS. PID alone still cannot select an instance.

## Deadlines, Clock shim, signals and cleanup

All ordinary and resumed Runs pass `--participant-timeout-ms 30000`
explicitly. Defaults are unchanged. This is a finite, unattended batch policy,
not permission for an indefinite human stop. The outer supervisor bounds
each Run at 60 s and the CI job at 25 minutes. Native callbacks are synchronous:
a Participant response timeout cannot interrupt a stopped runner. Process
and FMU Stops consume the runner's host response deadline. A separate control
uses an explicit 2 s deadline and holds the debugger for 3 s; Run failure
is the expected result. No equivalence is claimed at a deadline edge.

The shim case uses the same Process adapter with `shim=True`. The launch
script removes LD_PRELOAD from GDB and reinstates it only for the inferior.
GDB's own Python monotonic time remains host time; GDB reads the inferior's
actual `g_region->t`, frozen at 40/50 ms, and identifies its `clock_gettime`
symbol. It does not inject a clock call that could alter program state.
CPU-time clocks are outside this shimmed wall-clock claim.

| Action/control | Intended effect on Run |
| --- | --- |
| Continue / resume | Complete and byte-compare Recording with ordinary Run |
| Detach | GDB releases and resumes inferior; Run completes and compares equal |
| Kill inferior | Native runner dies, or Process exchange fails; no successful Run |
| SIGTERM to inferior | Native runner reports interruption; Python Process/FMU handler unwinds with exit 1 and the Step exchange fails |
| Debugger script error | Batch debugger exits; inferior is killed; Run fails |
| Hold beyond explicit Process deadline | Runner fails response exchange and cleans child group |
| Outer supervisor timeout | Kill supervised group and observed inferior group; retain partial evidence, fail proof |

GDB uses all-stop mode, no follow-fork debugging and a 16-frame stack bound.
Signals are not swallowed to manufacture success. Detach is different from
debugger failure: it deliberately releases a live process. The proof verifies
that no observed inferior PID survives cleanup, including detached Runs.
Raw stderr, debugger logs, diagnostics and independent command status remain
available for failed controls. Hard debugger/host SIGKILL before the first
saved stop is not qualified as recoverable capture.
GDB's default batch exit code reports command-script success, even when its
inferior exits 1. The Process wrapper can therefore exit 0 while the adapter
fails; the Step exchange still fails. `debugger-outcome.json` preserves the
inferior's actual exit separately from the runner report's wrapper status.

## Decision and retained evidence

**No change to SiL's debugging hooks.**
[Linux qualification run 39554858402](https://github.com/Stevie1704/sil/actions/runs/39554858402)
passed for PR head `78ee299b5f87b33c3c24cffe88631b8cb03c42af`, with checkout
merge revision `e7628efd21cc1c12f0e8ab9dfb9a8e0331a9e63d`. The host was Linux
7.0.0-1017-azure x86-64/glibc 2.39. All 43 experiments over Native, Process,
single-FMU, shimmed Process and the supplemental BUS group met their stop,
outcome, cleanup and equivalence checks; the forty-fourth established the grouped-ADAS
BUS-terminal rejection. External scripts plus the existing diagnostics-only
wrapper suffice for the qualified forms. No new launch or pause hook, ABI,
protocol, Manifest field or ADR change is justified by this evidence.

**The shared FMU group boundary is qualified.** Both group captures stopped
three distinct instances in one inferior PID (3560 by index, 3571 by Virtual
time), inside Activation 2 at 200 ms. The two node libraries and bus library
were mapped into that same process. All threads were stopped, with no child
FMU processes, and each inspected state stayed unchanged across a 50 ms
host pause. All four group Recordings matched byte-for-byte.

**Grouped ADAS remains unsupported by the current importer.** Even with two instances, an explicit BUS
profile and every Channel bound, the group importer rejects the ADAS
`camera.sample_time_ns`: it is no Tx_Data/Rx_Data member of a terminal. This
is a workload/importer limit before execution, not debugger failure. A future
supported multi-instance ADAS importer would need its own spec and tickets;
this does not weaken the separately observed BUS group's shared process boundary.

At both selected ADAS Activations, every ADAS form exposed valid held observations,
mode 1 (hazard), previous command and the output's selected radar ID 12,
target −3 and limited result. At 40 ms the held radar object was at 29 m,
relative speed −25 m/s, confidence 0.875; camera ID 4 confirmed it at 29.5 m,
y = 0.125 m, confidence 0.75. At 50 ms their x positions were 28.75 and
29.25 m. Held ego metadata had sequence 4/5 and origin 40/50 ms, validity 1;
discarded ego speed remained unavailable. The inline `limited` frame exposed
`from` = −2/−2.5 and `max_change` = 0.5. At the earlier selection stop,
the actual local `selected` was 12, `lead.x_m` was 29/28.75 and
`lead.relative_vx_mps` was −25. After the initializer, the inlined hazard
frame exposed `closing_mps` = 25. The aggregate `lead`, `lead.id`, `lead.y_m`
and `lead.confidence` remained optimized out; they are never reconstructed
from held inputs. At the later rate-limiter stop, `lead`, local `selected`,
local `target` and inline `to` were optimized out in all four forms.
Unavailable fields remain null with a reason; outputs are labelled separately.
Process returns land in libffi
without available caller source; the application entry/decision source and
stack remain captured.

The shim captures read the actual inferior region at 40,000,000 and
50,000,000 ns while GDB's host timestamps remained inside the runner's
Activation intervals. With ptrace_scope = 1 the sibling attach was refused
(raw diagnostic retained); every launch-under-GDB case worked. No host
security setting changed. Native SIGTERM produced an interrupted Run;
Process/FMU SIGTERM unwound the Python handler, and each explicit 2 s
deadline failed the selected Step at 40 ms. Every observed inferior was gone
after cleanup, including detach and debugger-error controls.

Within each form, two ordinary Runs, diagnostics-only, index-selected,
time-selected and detached Runs all produced byte-identical Recordings.
The full workflow artifact independently reproduces that comparison; the
retained subset keeps the comparison hashes and one Recording per form. The index/time
captures used different fresh PIDs/TIDs (Native 3256/3265, Process 3317/3327,
FMU 3396/3407, shim 3481/3491), host times and relocated stack addresses.
These observations establish Message reproducibility, not matching internal
thread history or instruction-level replay. No multithreaded application
schedule is qualified by this single-threaded workload.

The workflow uploads all binaries, separate debug files, sources, Manifests,
Recordings, observations and logs for 30 days. `digests.txt` hashes saved files;
`report.json` records the checkout revision, CI Run, tool/host versions, all
Run results and equivalence checks. [Retained evidence](evidence/README.md)
includes all selected stops, observations, one Recording per form, identities,
control logs and a Run summary with comparison hashes. Duplicate Recordings,
per-capture results, repeated command arrays and performance diagnostics are
omitted. The complete binary/debug/source bundle and raw reports are in
the workflow artifact; its published digest and expiry are retained too.

Repeated ordinary Runs must reproduce Messages; fresh processes, ASLR
addresses, TIDs and host scheduling need not match. Stack/thread snapshots
are observations, not instruction-level replay guarantees. There are no saved
historical variables, arbitrary reverse execution, variable histories or
checkpoints in an MCAP Recording. #196 owns checkpoint/resume. GDB's own
record/replay is a separate debugger facility, not a SiL feature.

No ABI, Step protocol, Manifest, report contract or ADR change is needed for
the external-script candidate. ADR 0004's diagnostics boundary remains intact.
The grouped-ADAS limitation would need a separately scoped importer/workload
decision before it could be qualified.

Primary references: [GDB optimized variables](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Variables.html),
[separate debug files](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Separate-Debug-Files.html),
[attach and detach](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Attach.html),
[record/replay](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Process-Record-and-Replay.html),
[pinned Ubuntu GDB package](https://packages.ubuntu.com/noble/gdb).
