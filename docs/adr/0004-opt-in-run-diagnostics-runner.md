# 4. An opt-in diagnostics runner with a versioned report

Date: 2026-10-18

Status: accepted

Relates to [issue #299](https://github.com/Stevie1704/sil/issues/299) and
[issue #300](https://github.com/Stevie1704/sil/issues/300). Replaces part of
DESIGN.md's #63 decision ("Metrics surface for pool and route counters").

## Context

DESIGN.md's #63 decision has no external metrics surface. Counters exist only
in the development binary `sil-run-instrumented`, its report has no stability
promise, and the production runner writes one artifact, the Recording. The
reason was determinism: a Run reproduces exactly, so a question about a Run is
answered by running it again in an instrumented build, and the production
routing path carries no counter, no branch and no reporting code.

Issue #299 asks for something #63 does not provide: a supported way to measure
the host execution cost of software in a Run (Task activations, Process Steps,
Replay publications, lifecycle phases) and to compare repeated fresh Runs. That
needs a public command and a report with a version and a schema.

## Decision

**Run diagnostics are a separate, installed runner, `sil-run-diagnostics`,
built from the same kernel sources with `SIL_RUN_DIAGNOSTICS`. It writes one
`sil_run_diagnostics` v1 report per Run.** The report is a supported contract
(docs/diagnostics-report.md).

This replaces only #63's prohibition of a supported observational report. The
rest of #63 stays:

- **The separate build stays.** `sil-run` has no clock read, counter or branch
  on the activation path; every collector hook compiles to nothing there
  (kernel/src/run_diagnostics.hpp). The Python CLI selects the diagnostics
  runner only when diagnostics are requested.
- **Observations never reach the Recording.** No value the collector measures
  enters MCAP content, the Manifest hash, scheduling or the exit code.
  `tests/test_determinism.py` asserts the same exit code, Recording bytes,
  stdout and stderr for both runners on every determinism fixture.
- **No metrics on stderr.** The report is a file. Only a failure to write it is
  one line on stderr, after the Run's own first diagnostic.
- **The copy-counter report stays internal.** `sil-run-instrumented` and
  `SIL_COPY_COUNTERS_OUT` keep no stability promise and are not installed.
- **Diagnostics, as #63 defines them, are unchanged.** A value needed to repair
  a failed configuration stays in the failure message of the ordinary runner.

### Error semantics of the diagnostics runner

| Situation | Exit code | Report |
| --- | --- | --- |
| The Run succeeds | 0 | Written, `run.exit_code` 0 |
| The Run fails (Participant, timeout, guard, interruption) | 1, as `sil-run` | Written with partial data and the first failure |
| Manifest or configuration error | 2, as `sil-run` | Written with preflight data only |
| The report path exists, or is the Recording, provenance or failure report path | 2, before the Manifest is loaded | Not written |
| The report cannot be written | The Run's own code | Not written; one stderr line follows the Run's diagnostic |
| Hard crash or SIGKILL | Signal status | Not written; a missing report is the evidence |

The report is written through a temporary sibling and linked into place, so a
reader never sees a partial file and an earlier report is never replaced.

The Python commands that build on this runner (#302, #303, #304) keep these
outcomes: a report failure never replaces a nonzero Run exit; if the Run
succeeded but a requested report could not be produced, the command exits 1;
invalid options or report input exit 2.

## Consequences

- An installed SiL carries two runners. The native archive, the staged install
  and the runtime image contain `sil-run-diagnostics` and the small
  `sil-diagnostics-launcher` a supervisor starts it through, so the runner's
  peak RSS is not a Linux high-water mark inherited from the supervisor
  (#301).
- Instrumentation overhead can move a Run that sits on a response-deadline
  edge. Inertness is asserted with deadlines far from the fixtures' response
  times; it is not claimed for every deadline.
- Timing values are observational. No test gates on a timing or memory
  threshold; tests pin identities, counts, structure and null reasons, and the
  statistics are pinned with synthetic durations.
- The #229 reference baseline and #125's gate are not changed by this
  decision.
