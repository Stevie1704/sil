# Actionable profiling workflow (#359)

This proof checks the combined offline improvements of #355–#358 using real
perf and Heaptrack captures from an installed wheel/native prefix. Run
[`qualify.py`](qualify.py) on Linux x86-64/glibc with the installed SiL runners,
perf and Heaptrack on `PATH`:

```sh
python proofs/actionable-profiling/qualify.py /absolute/new/evidence \
  --include /installed/native/include
```

The [workflow](../../.github/workflows/proof-actionable-profiling.yml) uses
Ubuntu 24.04 and Heaptrack `1.5.0+dfsg1-2ubuntu3`; perf comes from the running
kernel's tools package, whose exact package version is retained. It records
the initial host perf policy and explicitly changes
`kernel.perf_event_paranoid` to 2 on the disposable runner. The Python script
makes no policy changes. Builds use `-O2 -g` and frame pointers. Each command
has a 240-second limit and the job has a 30-minute limit. Raw perf stacks
are excluded from the uploaded artifact; the workload contains no secrets.

## Independent workload expectations

[`late_outlier.c`](late_outlier.c) registers 52 Tasks in one Native
Participant. Each has a 1 ms Period and 620 Activations. Task 51 at index 610
performs the known hot loop and eight separate 1,024-byte allocations, freed
immediately. All Tasks publish `(task,index,publication)` twice per
Activation. Thus the selected Virtual time is exactly 610,000,000 ns, the
capture has 32,240 intervals, and the query's half-open one-Slot window
contains 104 Messages. Its last two Messages are `(51,610,0)` and
`(51,610,1)`. These expectations come from the workload, not the renderer.
The loop result is deliberately absent from the Recording. The two builds
use 40/20 million loop iterations, preserving published bytes while changing
host work. Their binary/Manifest differences must remain visible.

The proof captures BASE with full provenance and NEW with external
provenance, retaining each policy and all profile-integrity hashes. It checks
Recording equivalence to an ordinary Run, exactly selects the late interval,
reconciles its execution-context sample count/weight with the normalized rows,
retains sampled leaf locations, and renders with digest-matched diagnostics.
No sample-presence or timing threshold is required: a missing sample remains
missing evidence. The heap site's independent expectation is eight
allocations/8,192 requested bytes; other runner allocation sites are not
attributed to the Native Task.

The bounded Recording query retains its source SHA-256 and all equal-time
publications. The receipt digest must equal the actual Recording digest.
It establishes publication identity, not a causal relationship to a sampled
function. Payload Sample time is not inferred. Each side also runs one
warm-up and three measured fresh diagnostic Runs as a separate benchmark.
The enriched diff keeps those experiments separate from the profiles.

The proof copies only the original minimal CPU/heap portable files into
different directories and renders those without optional diagnostics or
Recordings. Existing legacy/unknown policy and adversarial, recursive,
partial, zero-sample and browser fixtures remain in the repository test
suite; they complement real sampling, whose addresses and weights vary.

## Evidence and limits

`results.json` retains the source revision, host/libc/Python identity, worked
expectations, selected intervals and per-command elapsed observations.
`digests.json` records input/output sizes and SHA-256 values, including HTML.
GNU time records each command's peak RSS independently, including the offline
analysis/render commands. Its complete resource record is retained beside
each command's stdout/stderr. No time, RSS, sample share or overhead ratio
is a correctness gate. The loop edit supplies no significance verdict.

The existing [installed/container collector qualification](../profiling-qualification/README.md)
and [#329 hash-work evidence](../../docs/bench/)
remain the evidence for those unchanged collectors and provenance
implementation. This proof adds no collector, counter, runtime hook or
permanent processing-time field. Delivery B dispositions and reopening
criteria live in [the decision record](../../docs/profiling-investigation-decisions.md).

Retained real-Linux results are added here after the workflow succeeds;
macOS development checks cannot substitute for them.
