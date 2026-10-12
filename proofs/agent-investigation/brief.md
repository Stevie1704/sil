# Investigation brief

The nightly regression matrix of an ADAS radar/camera controller failed:
`ci/summary.json` and `ci/junit.xml` hold its result. Investigate each case
that `investigation.json` lists. For each case, identify what failed and
why. If you cannot, show why. Write one report per case to
`reports/<case>.json`.

This directory is your whole workspace. SiL is installed: `sil docs` lists
its offline docs, and `sil docs agent-guide` is the entry point. Do not read
files outside this directory.

## What you have

| Path | What it is |
| --- | --- |
| `investigation.json` | the cases, their bundles and lock digests, the allowed experiment parameters, the budgets |
| `ci/` | the output of `sil bundle matrix`: `summary.json`, `junit.xml`, `cases/<case>/` (the evidence of `sil bundle run`), `logs/` |
| `bundles/<case>/` | the sealed regression bundle of each case, with its Manifest, controller build, input Recording, prepared references and comparison contract |
| `tools/investigate.py` | the only way to run a command or an experiment |

The `late-private` case comes from a supplier. The supplier keeps its bundle
and its evidence private and shared only `ci/cases/late-private/summary.json`.

The target contract: each Run must repeat bit-identically, and the controller's
commands must match the prepared reference under the comparison contract of the
bundle (`cadence.contract.json`). The references are enumerated independently
of the controller. The contract, the references, the controller build and the
original bundles and evidence are fixed. Do not change them.

## Allowed actions

Run every command through the driver, from this directory:

```sh
python3 tools/investigate.py tool -- sil recording show ci/cases/.../run-1.mcap --channel X --json
python3 tools/investigate.py tool -- cat ci/summary.json
python3 tools/investigate.py status
```

The driver allows these commands: `sil recording`, `sil compare`,
`sil bundle verify`, `sil manifest validate`, `sil docs`, `sil fmi inspect`,
and readers such as `cat`, `head`, `grep` and `python3`. It counts the bytes
of output that it gives you. Read files through it, so that the count is
complete. Do not start a Run in a `python3` script: a Run is an experiment.

To test a hypothesis, run a follow-up experiment of a case with a bundle.
Write a spec into `specs/NAME.json`, then run
`python3 tools/investigate.py experiment specs/NAME.json`:

```json
{
  "sil_investigation_experiment": 1,
  "name": "NAME",
  "parent": "CASE",
  "hypothesis": "what you think is true, and what it predicts for this Run",
  "distinguishes": "which explanations predict a different result, and why",
  "channels": {"cadence.radar": {"latency_ns": 0, "interceptors": []}},
  "references": ["cadence.expected.mcap"]
}
```

- `channels` changes the input Channels of the parent's Manifest only:
  their `latency_ns` and their `interceptors`, in the form that the Manifests
  of the bundles use. A Channel you do not name
  keeps its parent setting. An empty `interceptors` list removes them.
- `references` names prepared references in the bundle. The driver compares
  the follow-up Run with each under the bundle's contract.
  `investigation.json` lists each prepared reference with the input-Channel
  settings it was enumerated for. A comparison is meaningful only when the
  follow-up has those settings. With no reference, you get a Recording to read
  and no verdict.

The driver writes a new bundle, seals it, runs it twice with `sil bundle run`
and writes `experiments/NAME/record.json`. The parent case does not change.

## Budgets

`investigation.json` gives the budgets: Runs, elapsed time from your first
driver call, tool calls, tool output bytes and inference tokens. The harness
counts the inference tokens of your session, and the driver counts the others. When a budget is exhausted,
the driver refuses every later action. Then write your reports with what you
have, and mark each unfinished case `incomplete`.

## Rules

- A matrix status is not a diagnosis. Find out whether the Run completed
  before you judge the controller's behavior.
- The first divergence of a comparison is a lead, not the cause.
- Something that happens at the same time as a failure is not its cause. A
  conclusion needs an experiment whose result your hypothesis predicts and the
  alternatives do not.
- Do not pass a case by a change of its contract, its references or its
  thresholds.
- Quote values exactly as the tools print them, with their location. Do not
  quote a value you did not read.
- When the evidence is not sufficient, the outcome is `inconclusive`. Name
  the evidence that is missing.

## Report format

```json
{
  "sil_investigation_report": 1,
  "case": "CASE",
  "outcome": "diagnosed | no-failure | execution-failure | inconclusive | incomplete",
  "summary": "two or three sentences",
  "observations": [
    {"id": "O1", "artifact": "ci/cases/CASE/runs/CASE/run-1.mcap",
     "sha256": "...", "channel": "cadence.command", "t_ns": 0, "ordinal": 0,
     "field": "mode", "value": 0, "statement": "what this value shows"},
    {"id": "O2", "artifact": "ci/summary.json", "sha256": "...",
     "pointer": "/cases/0/status", "value": "pass",
     "statement": "a value in a JSON file, at an RFC 6901 JSON Pointer"}
  ],
  "hypotheses": [
    {"id": "H1", "statement": "...", "predicts": "..."}
  ],
  "experiments": [
    {"name": "NAME", "tests": ["H1", "H2"], "result": "...",
     "observations": ["O3"]}
  ],
  "conclusions": [
    {"statement": "...", "supported_by": ["O1", "NAME"]}
  ],
  "unresolved_alternatives": ["..."],
  "missing_evidence": ["..."],
  "regression_case": "NAME or null"
}
```

| `outcome` | Use it when |
| --- | --- |
| `diagnosed` | a behavioral requirement failed, and an experiment supports the cause |
| `no-failure` | the case passed, and nothing needs a diagnosis |
| `execution-failure` | the Run did not complete, so no behavioral verdict exists |
| `inconclusive` | the evidence does not decide; `missing_evidence` names what is missing |
| `incomplete` | a budget ended the investigation first |

- `t_ns` is the publication time that `sil recording show` prints as `t_ns`.
  `ordinal` counts Messages of the Channel at that same `t_ns`, from 0. For
  an array field, add `"index"`.
- `sha256` is the digest of the artifact file when you read it.
- `regression_case` names the experiment that preserves the failure as a
  reproducible regression case, with the bundle's contract unchanged.
