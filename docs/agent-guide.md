# Agent guide

This guide is the entry point for a coding agent that helps a user test a
system under test (SUT) with SiL. Read it with `sil docs agent-guide`. These
docs are for the installed version: `sil docs` lists the topics, and
`sil docs --path` prints their directory, so you can search them with `grep`.

To let a coding agent find SiL without this guide, run `sil skill install` in
the project. It writes the agent skill of the installed version to
`.agents/skills/sil/SKILL.md` and links `.claude/skills/sil` to it. The agent
then loads the skill for SUT, FMU, Manifest, Recording and Run tasks.
`--user` installs it into the home directory, and `--dir DIR` into `DIR`.
Run the command again after you upgrade SiL: the skill names its version.

For the guides' adapter, binding and regression preparation files, run
`sil examples --dir examples` in a new working directory. Then run their
`examples/...` commands there. `sil docs examples` describes the exported
sources and the native installation or compiler each workflow needs.

## What SiL is

SiL runs virtual ECUs (vECUs) and models deterministically in Virtual time.
One Run executes one Manifest and writes one Recording. The same artifacts on
the same machine class give bit-identical Recordings. The terms in this guide
(Run, Manifest, Participant, Channel, Schema, Recording) have exact meanings:
read `sil docs glossary`.

| Part | What it is |
| --- | --- |
| Manifest | A canonical JSON file, SHA-256 hashed, the single input of a Run. Write it with the `sil.manifest` Python builder. |
| Participant | The code that a Run schedules: a Native participant (a shared library against the C ABI), a Process participant (an executable that speaks the Step protocol), or a Replay participant. |
| Channel | A typed connection between Participants. Its Schema gives the payload layout. |
| Recording | An MCAP file with virtual timestamps only. |

## Onboard a SUT

Do the steps in this order. Each step names the topic to read.

1. **Inspect the SUT.** Find out how it can join a Run:
   - An FMU: run `sil fmi inspect model.fmu --json`. Read `sil docs fmi`.
   - A C library: read `sil docs library`. For a large C interface, `sil schema
     import` writes a Schema from the DWARF data of the object file (it needs
     the `sil[dwarf]` extra).
   - An executable: it must speak the Step protocol. Read
     `sil docs step-protocol`. A Python Participant uses `sil.participant`.
   - A C application against the Native ABI: read `sil docs adas-reference`.

   To start a new Participant, run `sil new participant --process NAME` or
   `sil new participant --native NAME`. It writes a minimal Participant and a
   `manifest.py` script. This script writes a Manifest that runs the
   Participant. The result runs and passes `sil check` with no edits. Read `sil docs running`.
2. **Author the Manifest.** Use the `sil.manifest` builder (`Manifest`,
   `add_schemas`, `add_channel`, `add_process`, `write`). `sil docs
   getting-started` names the packaged ACC example
   (`python -m sil.examples.acc.manifest acc.json`), a complete Manifest to
   copy from. For FMUs, `sil fmi couple`, `sil fmi replay` and
   `sil fmi substitute` write the Manifest for you: read `sil docs
   fmu-coupling` and `sil docs recorded-input`. `sil-run` finds a bare
   Process participant command, such as `python3`, on its own `PATH`. So
   activate the environment that has `sil` installed before the Run, or the
   Run fails with `No module named 'sil'`. To select between a bare name and
   an absolute path, read the Process participant command section of `sil
   docs running`.
3. **Validate.** `sil manifest validate manifest.json --json` checks the
   Manifest without a Run. `sil-run` stops at the first error, but this
   command lists every error with its JSON path and a fix hint. Fix all
   errors, then run. Exit
   code `0` is valid, `2` is not. Read `sil docs running`.
4. **Run.** `sil run manifest.json -o out.mcap` starts the native `sil-run`,
   and `sil check` uses it too. The wheel does not contain `sil-run`: it must
   be on `PATH` from a native installation, or you run in the container image
   (read `sil docs container`). Read `sil docs running` for the exit codes,
   the guards and the Clock shim.
5. **Check.** `sil recording channels out.mcap` lists the Channels of the
   Recording, and `sil recording show out.mcap --channel NAME --from T --to T`
   prints what a Channel carried in a virtual-time interval, decoded with its
   Schema: read `sil docs running`. `sil recording query` projects fields,
   summarizes numeric values, or locates a comparison or threshold crossing
   with bounded output and an explicit completion report: read
   `sil docs recording-queries`. `sil check manifest.json` runs the
   Manifest twice and fails on any Recording difference. It reports the first
   difference, and `--keep DIR` keeps both Recordings. `sil compare`
   compares a Recording with a reference under a contract: read `sil docs
   comparison`. To keep a case for CI, seal it as a regression bundle: read
   `sil docs regression-bundles`.

## Investigate a failed Run

Use these steps when a Run, a regression bundle or a matrix case fails. They
use only the commands of this guide.

1. **Classify the failure first.** A matrix status is not a diagnosis:
   `behavioral-failure` also includes a Participant that missed its response
   deadline. Read the `exit_code` of each Run in the bundle `summary.json`. A
   Run that exited `1` and wrote `<recording>.failure.json` did not complete.
   Its `category`, `participant` and `virtual_time_ns` tell what stopped it.
   Such a Run has no comparison, so it has no behavioral verdict. A Run that
   exited `0` and failed a comparison is a behavioral failure.
2. **Find where it starts.** Read `<comparison>.comparison.json`. Its
   `first_divergence` gives the Channel, the field, the observation time and
   the publication time. It is a lead, not the cause. Read the other fields
   and the inputs near that time with `sil recording show --channel NAME
   --from T --to T --json`. Its `t_ns` is the publication time. A Sample time
   is a payload field: do not mix the two.
   For long Recordings, use `sil recording query --channel NAME --field FIELD
   --where FIELD cross-up LIMIT --max-results 1` to locate a threshold crossing,
   then project nearby inputs in a half-open publication-time window. Check
   `scan.complete` and `scan.matches`, since empty returned results can also
   mean truncated output or an incomplete scan. Summaries are not a substitute
   for exact predicate scans. The versioned query report supplies the source
   digest and Message index needed to cite equal-time evidence; read
   `sil docs recording-queries` for its window and crossing context rules.
3. **Know what the evidence covers.** A failed Run can leave a partial
   Recording: `sil recording channels` gives the last time of each Channel.
   Make no statement about a time after it. When a Recording, a report or a
   bundle is missing, say so. Do not fill the gap.
4. **Test a hypothesis with a new Run.** For each hypothesis, write down what
   it predicts for a Run that you can do. Select a Run for which the
   hypotheses predict different results. Copy the bundle into a new
   directory. Change only its Manifest. Seal the new bundle. Run it with
   `sil bundle run` into a new evidence directory. Do not change the original
   bundle, its evidence, its comparison contract or its references. A
   tolerance that you relax until the case passes is not a diagnosis.
5. **Keep the regression case.** Keep the sealed follow-up bundle that shows
   the failure, with the original contract and references. Add it to the
   matrix with its lock digest.
6. **Report.** Keep observations (artifact digest, Channel, field, `t_ns`,
   value), hypotheses, experiments, conclusions and open alternatives apart.
   Two events at the same time do not show a cause. When the evidence does
   not decide, the result is "inconclusive", with the evidence that is
   missing.

[proofs/agent-investigation](../proofs/agent-investigation/README.md) shows
these steps on four cases, with a real agent trial and checks of its claims.

## Measure a Run

Use these steps when the user asks how fast a Run is, which Participant is
slow, or how much CPU time or memory a Participant uses. Read
`sil docs running`, the sections from "Measure a Run" to "What the
measurements mean", and the contract in `sil docs diagnostics-report`.

1. **Select the command.** For one Run, use `sil run MANIFEST -o run.mcap
   --diagnostics`. It writes `run.mcap.diagnostics.json`. To compare values or
   to see their spread, use `sil benchmark MANIFEST --out DIR`. It writes
   `DIR/summary.json`. The default is 1 warm-up Run and 5 measured Runs. Do
   not compare a value of one Run with a value of another configuration:
   use `sil benchmark` for both.
2. **Read the JSON, not the stdout summary or the HTML report.** The summary
   text can change in any release, and the HTML report is for people. Check
   the exit code first. A Run report with `completeness.run_complete` false,
   or a benchmark with `status.complete` false, has partial values.
3. **Find the values for the question.**

   | Question | Run report member | Benchmark member |
   | --- | --- | --- |
   | How long does the Run take? | `observational.run_wall_time.wall_ns`, `observational.phases` | the `run_wall_ns` and phase metrics in `aggregates.metrics` |
   | Is the Run faster than real time? | `observational.virtual_time_speed.scheduled_factor` | the `scheduled_factor` metric |
   | Which Task or Step is slow? | `observational.tasks`: `timing.p50_ns`, `p95_ns`, `max_ns` and `slowest` of each row | the Run reports in `runs[].report` |
   | How much CPU time or memory does a Participant use? | `observational.resources.process_participants`, and `runner` for Native participants | the `process_*` metrics, with `participant`, and the `runner_*` metrics |

4. **Treat an unavailable value as unknown.** A null value has its reason in
   the `unavailable` map next to it. Do not use zero or an estimate instead.
5. **Do not make these conclusions.**
   - The values come from the instrumented `sil-run-diagnostics`. They are
     not timings of `sil-run`.
   - A Process Step time includes IPC and waiting for the operating system.
     It is not the time of the algorithm alone.
   - Do not add the peak memory of different processes. Native participants
     have no record of their own: their cost is in the `runner` record.
   - Do not add or average percentiles of different Runs. A maximum is not a
     worst-case execution time.
   - A timing value does not show if a Run is deterministic. Use `sil check`.
6. **Report** each value with its unit, its scope, the host from the report
   `host` member, and the number of measured Runs.

## Profile a Run

Use these steps when the user asks which function or allocation site inside
a Participant is expensive, after "Measure a Run" named the Task or Step.
Read `sil docs running`, section "Profile a Run", and the contract in
`sil docs profile-report`.

1. **Check the host.** Linux x86-64/glibc with perf (CPU) or Heaptrack
   (heap) installed. If `sil profile` stops in preflight (exit `1`), report
   the missing tool or permission that stderr names. The host owner decides
   a host change; `sil profile` makes none.
2. **Select one collector and one scope.**
   - CPU: `sil profile MANIFEST --out DIR --collector cpu --activations`.
     Add `--modules-from ARCHIVE` for each FMU archive, or its functions
     have no names.
   - Heap: `--collector heap --runner` for Native participants (all in one
     scope), or `--participant NAME` for one unshimmed Process participant
     or FMU group.
3. **Read `DIR/profile.json`.** Check the exit code, then `run.status` and
   `capture.status` separately: a Run failure keeps its own exit code. A
   capture that is not `complete` has partial evidence; its `reasons` say
   why.
4. **Find the values for the question.**

   | Question | Member |
   | --- | --- |
   | Which function uses CPU? | `capture.hotspots.self` and `inclusive` (sample weights, an estimate) |
   | Which Task or Step ran the samples? | `activation_correlation.summary.tasks` and `samples` (classes) |
   | Which site allocates or churns? | `capture.analysis.hotspots.self`: `allocations`, `allocated_bytes`, `temporary_allocations` |
   | Are names and lines trustworthy? | `capture.modules[].debug.status`, `capture.symbolization` |

5. **State these limits with the result.** A sample weight is not a function
   duration. `window_associated` is not proven Step work. Outstanding bytes
   at capture end are not a leak. There is no allocation history per Step
   and no total per Native participant or FMU instance. A profile describes
   this host build.
6. **Explore offline.** Run `sil profile render DIR/profile.json --html
   profile.html`. The CPU flame graph uses sampled-weight widths, supports
   zoom/search, and shares the explorer filters. With `--activations`, select
   an interval in the timeline to inspect its samples; links from the Task
   table and timeline open a Task drill-down. Keep `execution_context` and
   `window_associated` separate. Read the denominators, zero-sample
   Activations, incomplete observation regions and bounded remainders. The
   exact gap boundaries are not retained; blank space is not idle CPU time.
7. **Compare captures.** Use `sil profile diff BASE/profile.json
   NEW/profile.json --html diff.html`, with both portable sets. Check the
   identities and warnings first. CPU compares shares with both denominators;
   per-Task shares require complete correlation on both sides. Heap compares
   allocation metrics per site. One Run each supplies no significance
   verdict, and small share changes may be sampling noise. Exit `0` means
   rendered, `1` output write failed, `2` invalid/missing or mismatched input
   or an output collision. Read `sil docs profile-report` for bounds.
8. **Hand over** `profile.json` with the normalized files that it names, or
   the HTML of `sil profile render`. Share `cpu/perf.data` only as a core
   dump: it holds copies of stacks.

## Machine-readable output

Use exit codes, not message text. Message text can change in any release.

| Command | Output |
| --- | --- |
| `sil fmi inspect --json` | the `sil_fmi_inspection` report |
| `sil manifest validate --json` | the `sil_manifest_validation` report |
| `sil check --json` | the `sil_check_report` report |
| `sil compare --json` | the `sil_comparison_report` report |
| `sil recording channels --json` | the `sil_recording_channels` report |
| `sil recording show --json` | one `sil_recording_show` JSON object per line and per Message |
| `sil recording query` | a bounded `sil_recording_query` version 1 report, including scan/output completion and source/evidence identity |
| `sil recording csv --receipt`, `sil recording window --receipt` | a JSON receipt |
| `sil fmi couple --receipt`, `sil fmi replay --receipt`, `sil fmi substitute --receipt` | a JSON receipt |
| `sil run` | the Recording and its `<recording>.provenance.json` side-car |
| `sil run`, on a failed Run | the `<recording>.failure.json` report: which Participant failed, how, at which Step and virtual time, and its stderr tail (`sil docs running`) |
| `sil run --diagnostics` | the `sil_run_diagnostics` report, `<recording>.diagnostics.json` by default, and a summary on stdout (`sil docs running`, `sil docs diagnostics-report`) |
| `sil benchmark --out DIR` | `DIR/summary.json`, a `sil_benchmark_report` with every Run report embedded, a directory of evidence per Run, and a summary on stdout (`sil docs running`, `sil docs diagnostics-report`) |
| `sil profile --out DIR` | `DIR/profile.json`, a `sil_profile_report`, with the raw capture and the normalized files it names, and a summary on stdout (`sil docs profile-report`) |
| `sil profile diff BASE/profile.json NEW/profile.json` | A terminal comparison, optionally standalone HTML with `--html`; shares, denominators, identities and warnings (`sil docs profile-report`). Message text is not a machine-readable contract. |
| every command | the exit code: `sil docs running` lists those of a Run |

`sil docs support` says which interfaces and report versions are stable, and
which are implementation details.
