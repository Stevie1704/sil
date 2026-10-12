# Agent investigation of a failed regression

Issue [#283](https://github.com/Stevie1704/sil/issues/283), part of the
roadmap [#281](https://github.com/Stevie1704/sil/issues/281). An agent gets a
failed regression matrix and its retained artifacts. It finds the evidence,
states hypotheses that it can test, runs controlled follow-up experiments and
writes a report with evidence references. Executable checks then test every
claim of the report that they can test.

The proof uses the commands that exist today: `sil bundle`,
`sil bundle matrix`, `sil compare`, `sil recording` and the failure report of
#272. #272 is closed and on `main` (commit 7b6c09a), but it is not in the
release v0.1.0, so the proof needs a build from `main`. It adds no runner and
no verdict engine. The proof driver only
restricts and counts what the agent does, and it composes follow-up bundles
from those commands.

## Cases

All cases replay the `cadence` maneuver of the
[ADAS reference application](../../docs/adas-reference.md) into one
controller build. They use that example's comparison contract and its
independently enumerated references. The build under investigation is
`adas_reference_arrival_time.so`, which is copied into each bundle as
`adas_reference.so`. The cause stays in the evaluator's
[`reference.json`](reference.json), and the agent never gets that file.

| Case | Run | Matrix status | Expected outcome |
| --- | --- | --- | --- |
| `delay-regression` | the `delay` experiment: radar lists published in [60, 100) ms arrive 50 ms late | `behavioral-failure`: the comparison fails | `diagnosed` |
| `nominal-control` | no fault | `pass` | `no-failure` |
| `monitor-timeout` | no fault, plus a command monitor ([`monitor.py`](monitor.py)) that blocks at 100 ms | `behavioral-failure`: Run exit 1, failure report category `timeout` | `execution-failure` |
| `late-private` | 10 ms input Latency, in a supplier's private bundle | `behavioral-failure`; only its `summary.json` is shared | `inconclusive` |

The matrix status `behavioral-failure` covers both a failed comparison and a
missed response deadline. To tell them apart, the agent must read the Run's
exit code and failure report. The first divergence of `delay-regression` is
`mode` at observation 120 ms. That is a consequence, not the cause.

## Files

| File | What it does |
| --- | --- |
| [`prepare.py`](prepare.py) | Builds and seals the four bundles from the source tree, runs the matrix with the installed `sil`, removes the private case's evidence except its summary, and writes the agent workspace and the evaluator directory |
| [`brief.md`](brief.md) | The agent's problem statement: the workspace, allowed actions, budgets, rules and report format. It does not name a cause. |
| [`investigate.py`](investigate.py) | The proof driver. `tool` runs an allowed read-only command and counts the bytes it delivers. `experiment` composes, seals and runs a follow-up bundle and writes its record. `status` shows what was used. Every action goes into `ledger.jsonl`. |
| [`verify.py`](verify.py) | Checks a workspace's reports against the files, independent of the agent's tools |
| [`retain.py`](retain.py) | Copies a finished trial into `evidence/` without binaries, and keeps its verification |
| [`reference.json`](reference.json) | The evaluator's expected outcomes and known causes |
| [`evidence/trial-1/`](evidence/trial-1/) | The retained real-agent trial |

## Reproduce

From the source tree, with a built kernel (`make build`) and a staged
installation: the wheel in its own venv and the CMake install prefix, as
[getting started](../../docs/getting-started.md) shows. The supported
acceptance platform of regression bundles is Linux x86-64.

```sh
cmake --install build --prefix "$PREFIX"
# VENV holds the stamped wheel, installed as in docs/getting-started.md.
PYTHONPATH=python/src python3 proofs/agent-investigation/prepare.py "$OUT" \
    --python-bin "$VENV/bin" --sil-bin "$PREFIX/bin" \
    --target build/adas_reference_arrival_time.so
# Give the agent "$OUT/agent" and brief.md; keep "$OUT/evaluator" from it.
"$VENV/bin/python" proofs/agent-investigation/retain.py "$OUT" \
    proofs/agent-investigation/evidence/trial-N
```

`tests/test_agent_investigation.py` runs in the ordinary suite, on Linux
x86-64 in CI. It checks the following offline, and it calls no model:

- the four case statuses, the failure report of the timeout and the private
  case's shared evidence;
- a follow-up experiment: a new sealed bundle with the parent's contract and
  references, its record, and an unchanged parent;
- refused commands, refused experiment specs, and an exhausted budget that
  allows an `incomplete` report;
- `verify.py` accepts canned true reports, and rejects each false claim:
  an invented value, a wrong digest or time, an outcome that the facts do not
  support, a missing regression case, a dangling reference, a changed original
  and an experiment that changes more than its inputs;
- the retained trial against its retained evidence. It also repeats the
  trial's experiments from their specs, and requires the same verdicts, first
  divergences and quoted Recording values.

A canned report is a fixture for the checks, not evidence of diagnostic
ability. Only a retained real-agent trial is that evidence.

## Allowed actions and budgets

The agent runs commands only through `investigate.py`. It allows
`sil recording channels|show`, `sil compare`, `sil bundle verify`,
`sil manifest validate`, `sil docs`, `sil fmi inspect`, shell readers and
`python3`. All path arguments must be in the workspace. A `sil` command that
executes a Run is refused (exit 64). Follow-up Runs are experiments.
`python3`, `find` and `sort` can still write files or start a Run. The driver
cannot stop that, but the ledger records each command line, and `verify.py`
fails a ledger in which a command line starts a Run. An
experiment can change only the input Channels' `latency_ns` and
`interceptors`. It can compare only with prepared references, under the
parent's contract.

| Budget | Value | Enforced by |
| --- | --- | --- |
| Runs | 16 (an experiment runs its Manifest twice) | the driver |
| Elapsed time from the first action | 3600 s | the driver |
| Tool calls | 200 | the driver |
| Tool output bytes delivered | 2 000 000 (65 536 per call; a longer output is cut with a notice) | the driver |
| Inference tokens | 400 000 | the evaluator, from the harness's token total after the session |

When a budget is exhausted, the driver refuses every later action (exit 65)
and tells the agent to report each unfinished case as `incomplete`. The
completed experiments stay in the workspace.

## What `verify.py` checks

- Each quoted Recording value is decoded again from the MCAP file, with the
  stored Schema and the script's own decoder, at the quoted Channel,
  publication time, same-time ordinal and field. Each quoted JSON value is
  read again at its JSON Pointer. The artifact digest must match.
- Each experiment record is checked against its spec, Manifest, lock and
  summary digests and its parent lock. The contract and references must be
  the parent's. The Manifest may differ from the parent's only on the input
  Channels' Latency and Interceptors. The determinism hashes are compared
  with the Recordings. Each comparison is computed again with `sil.compare`,
  and its first-divergence values are decoded again.
- The outcome must fit the facts. `no-failure` needs a passed case.
  `execution-failure` needs a failure report. `diagnosed` needs a completed
  Run, verified experiments and a preserved regression case that fails
  against the parent case's references. `inconclusive` needs the missing
  evidence, and `incomplete` a budget refusal.
- The ledger is within the Run, elapsed-time, tool-call and output budgets,
  or has a budget refusal. No command line in it starts a Run outside an
  experiment.
- Every file that preparation wrote is unchanged. Each outcome is compared
  with the evaluator's reference.

Whether the agent found the known cause is the evaluator's judgement. It is
in `assessment.json`, apart from the executable checks.

## Trial 1

One agent session: Claude Code, a general-purpose subagent with no earlier
context, model `claude-opus-5-5`, on macOS arm64 from a staged installation.
[`agent.json`](evidence/trial-1/agent.json) records the harness, the settings and
the usage. [`prompt.md`](evidence/trial-1/prompt.md) is the whole prompt.

| Case | Reported outcome | Expected | Known cause found |
| --- | --- | --- | --- |
| `delay-regression` | `diagnosed` | `diagnosed` | yes, with the caveat below |
| `nominal-control` | `no-failure` | `no-failure` | not applicable |
| `monitor-timeout` | `execution-failure` | `execution-failure` | yes: the monitor's response deadline at 100 ms |
| `late-private` | `inconclusive` | `inconclusive` | not applicable: the missing evidence is named |

The diagnosis rests on three experiments:

- `delay-repro` reproduces the failure and is the preserved regression case.
- `late-probe` uses 10 ms input Latency, no Interceptor, and compares with
  `cadence.late`. It fails with 57 divergences, all on the three age fields,
  each 10 ms short. The arrival-time hypothesis predicts this result. A fault
  of the delay Interceptor or of queued lists predicts a pass.
- `nominal-in-delay-bundle` passes without the Interceptor.

The agent kept the 57 divergences of the private case as "consistent", and
did not use them as a conclusion.

| Measure | Value |
| --- | --- |
| Tool calls / output bytes delivered | 48 / 189 423 |
| Experiments / Runs | 3 / 6 |
| Refusals | 1 (a `sh -c` pipeline) |
| First to last action (wall clock) | 14:03:16 to 14:08:07 UTC |
| Harness usage | 123 134 subagent tokens, 28 tool uses, 311 s |

Trial 1 ran before the inference budget had a number. Its brief named "one
agent session" as the inference budget. Its 123 134 tokens are within the
400 000 tokens that later trials get.

[`verification.json`](evidence/trial-1/verification.json) has 231 passing
checks and 1 failing check. In observation O7 of `monitor-timeout`, the agent
quoted a line of `monitor.py` source text. The report format cannot cite
source text, so no executable check can confirm that quote. Every other
quoted value, comparison, record and outcome holds.

Limits of this evidence:

- **The docs give a hint.** Before its experiments, the agent read
  `sil docs adas-reference` (ledger 21, 23 and 24). That doc describes an
  arrival-time build of the reference as a control. So this trial does not
  show that an agent finds this cause without that hint. The experiment that
  decides and the checked values do not depend on the hint.
- **The isolation is by instruction.** The driver refuses paths outside the
  workspace, but the agent's own file tools and the `python3` reader could
  reach further. The prompt prohibited this, and the ledger records what the
  agent ran through the driver.
- **One trial, on macOS arm64.** The supported acceptance platform is Linux
  x86-64. CI repeats the trial's experiments there and requires the same
  values. Repeated agent trials can give other results. Assess them apart
  from the deterministic Run checks.
- **Partial counts.** The driver counts the bytes that it delivers. The agent
  also filtered some output in its own shell, which the count does not
  include. The inference cost is the harness's token total only.

## Findings for #282 and the tools

The investigation needed none of the bounded queries of
[#282](https://github.com/Stevie1704/sil/issues/282). Each Recording has 20
activations, so `sil recording show --channel` printed a whole Channel in
about 4 KB. Nothing was adopted, so this trial cannot show the effect of
#282. The friction the trial showed:

| Friction | Evidence | Where it belongs |
| --- | --- | --- |
| No list of every divergence, or of the divergence count of each field | ledger 51 and 52: `python3` scripts over `sil recording show --json` to count divergences per field | `sil compare` (a full divergence list) or the summaries of #282 |
| A whole doc topic is the smallest unit of `sil docs` | ledger 21, 23 and 24: three reads of the 35 KB `adas-reference` topic, 56 % of all delivered bytes | a section or search option of `sil docs` |
| A report cannot cite a line of text | observation O7 of `monitor-timeout` cannot be checked | the report format of this proof |
| An experiment can change only input Channels | the agent could not test a non-blocking monitor | intended for this proof; a wider follow-up is #284 |
| The matrix gives a missed response deadline the status `behavioral-failure` | `ci/summary.json` of `monitor-timeout`; the agent's report calls the status misleading | `sil bundle matrix`: a status apart from a failed comparison, documented today in `sil docs regression-bundles` |

The proof does not need event search and threshold crossings at this scale.
Bring them back to #282 only with a long Recording on which an investigation
needs them.

## Not in scope

No campaign framework (#284), no live observation (#285), no change to the
kernel, and no claim that an agent can diagnose any failure. The skill of
#277 (`sil skill install`) now names the guide section
[Investigate a failed Run](../../docs/agent-guide.md#investigate-a-failed-run),
which states this workflow with the public commands. The skill does not copy
the section.
