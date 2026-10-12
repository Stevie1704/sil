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
   Schema: read `sil docs running`. `sil check manifest.json` runs the
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
3. **Know what the evidence covers.** A failed Run can leave a partial
   Recording: `sil recording channels` gives the last time of each Channel.
   Make no statement about a time after it. When a Recording, a report or a
   bundle is missing, say so. Do not fill the gap.
4. **Test a hypothesis with a new Run.** For each hypothesis, write down what
   it predicts for a Run that you can do. Select a Run for which the
   hypotheses predict different results. Copy the bundle into a new
   directory, change only its Manifest, seal it, and run it with `sil bundle
   run` into a new evidence directory. Do not change the original bundle,
   its evidence, its comparison contract or its references. A tolerance that
   you relax until the case passes is not a diagnosis.
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
| `sil recording csv --receipt`, `sil recording window --receipt` | a JSON receipt |
| `sil fmi couple --receipt`, `sil fmi replay --receipt`, `sil fmi substitute --receipt` | a JSON receipt |
| `sil run` | the Recording and its `<recording>.provenance.json` side-car |
| `sil run`, on a failed Run | the `<recording>.failure.json` report: which Participant failed, how, at which Step and virtual time, and its stderr tail (`sil docs running`) |
| every command | the exit code: `sil docs running` lists those of a Run |

`sil docs support` says which interfaces and report versions are stable, and
which are implementation details.
