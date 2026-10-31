# Provenance qualification

This report closes [#329](https://github.com/Stevie1704/sil/issues/329) of the
parent spec [#323](https://github.com/Stevie1704/sil/issues/323). It gives the
integrated measurements, the consumer audit and a map from each acceptance item
to its evidence or to a limitation. It is evidence, not a threshold: no test or
gate reads a duration from it.

## Summary

- Most of the `full` mode gain comes from the SHA-256 backend. Repeated-input
  reuse adds a large gain only where inputs repeat.
- The Recording digest during the write moves about 45 ms of 72 MiB hash work
  from finalization to execution. It has **no resolved end-to-end gain**.
- `external` mode removes the remaining provenance hash work. Manifest hash and
  Replay verification stay.
- A profile command gains from the faster runner. Its own artifact hash stays,
  also in `external` mode. With Activations, the hash work falls by half.
- **All timings are from arm64.** No x86-64 timing exists yet
  ([limitations](#limitations)).

## Conditions

| Item | Value |
| --- | --- |
| Machine | Apple M1 Pro, macOS 27, arm64. This is not the supported machine class. |
| Build | CMake Release, each revision in its own clean clone |
| Load | This agent only, with no other build or test. The desktop session stayed open. The 1-minute load average was 2.8 to 7.0 during the captures, and about 4 when idle. The earlier captures had load averages to 18. |
| Cache | Warm page cache. There is no cold-cache series, because the tool cannot control one. |
| Repetitions | 9 for each series, 5 for the Linux Heaptrack series |

Each capture is at [hash-baseline-329-*](hash-baseline.md#retained-captures) as
tables and raw JSON. The `conditions.load` member of each JSON file has the load
at its start.

## Attribution of the Run

Each series adds one change, in the order of the integration branch. Thus the
difference between two adjacent columns is the effect of one change.

| Column | Revision | Adds |
| --- | --- | --- |
| Base | `600fa429` | Nothing: the pinned revision |
| Backend | `558f993` | OpenSSL EVP |
| Policy | `16c2683` | Schema v2 and the option, in `full` mode |
| Dedup | `693594b` | One digest for each canonical input |
| Streaming | `67fee44` | The Recording digest while writing, in `full` mode |
| External | `67fee44` | The same build in `external` mode |

### Ordinary `sil-run`, end to end

Median, with min to max, in ms.

| Workload | Base | Backend | Policy | Dedup | Streaming | External |
| --- | --- | --- | --- | --- | --- | --- |
| `native-shared-library` | 1356.4 (1351.7-1369.4) | 172.7 (171.7-182.5) | 171.9 (170.9-181.6) | 16.6 (16.3-17.2) | 16.1 (15.9-17.1) | 8.0 (7.6-8.7) |
| `native-distinct-artifacts` | 2714.7 (2693.6-2774.9) | 346.4 (345.6-392.6) | 347.3 (343.8-362.3) | 346.8 (344.6-368.2) | 346.6 (344.6-369.6) | 12.0 (9.2-26.4) |
| `native-recording-heavy` | 643.0 (640.5-663.4) | 314.0 (310.5-333.3) | 309.0 (306.5-317.0) | 311.3 (308.8-314.7) | 307.9 (305.2-309.1) | 261.7 (259.0-263.6) |
| `process-shared-inputs` | 1775.5 (1727.8-1782.9) | 580.9 (511.1-622.2) | 617.6 (566.6-707.4) | 432.1 (383.9-471.8) | 455.8 (388.3-478.9) | 425.0 (373.3-445.8) |
| `replay-verification` | 118.5 (117.3-120.1) | 50.5 (50.2-51.7) | 50.8 (50.2-51.1) | 50.9 (50.4-51.3) | 50.6 (50.2-53.0) | 42.6 (42.2-43.0) |

### Hash work

Operations and MiB from the read trace of the diagnostics runner. The size of
the runner increases by about 4.2 MiB with the backend, and the runner is one of
the inputs.

| Workload | Role | Base | Backend, Policy | Dedup | Streaming | External |
| --- | --- | --- | --- | --- | --- | --- |
| `native-shared-library` | Inputs | 33, 257.0 | 33, 261.2 | 2, 13.2 | 2, 13.2 | 0, 0 |
| `native-distinct-artifacts` | Inputs | 5, 513.0 | 5, 517.2 | 5, 517.2 | 5, 517.2 | 0, 0 |
| `native-recording-heavy` | Output pass | 1, 72.1 | 1, 72.1 | 1, 72.1 | 0, 0 | 0, 0 |
| `process-shared-inputs` | Inputs | 25, 257.4 | 25, 261.6 | 4, 37.2 | 4, 37.3 | 0, 0 |
| `replay-verification` | Replay verification | 1, 7.2 | 1, 7.2 | 1, 7.2 | 1, 7.2 | **1, 7.2** |
| All | Manifest | 1 | 1 | 1 | 1 | **1** |

In each workload of the Streaming and External columns, the digest is correct
without an output pass. In the External column there is no digest. Each capture
checks the digests against `hashlib`, and all Runs of a workload wrote equal
bytes.

### Phases of `native-recording-heavy`

Attributed series, median in ms. Do not compare a single row without the end to
end time: a move across a boundary is not a saving.

| Value | Dedup | Streaming | External |
| --- | --- | --- | --- |
| Preparation | 4.6 | 4.4 | 0.9 |
| Execution and Recording | 288.6 | 332.6 | 290.3 |
| Finalization | 48.7 | 1.7 | 1.2 |
| Output hash pass | 47.4 | 0 | 0 |
| Reported Run wall | 295.0 | 338.8 | 292.4 |

### Result for each change

| Change | Measured effect on this machine | How to read it |
| --- | --- | --- |
| Backend | End to end: 1356 to 173 ms, 2715 to 346 ms, 643 to 314 ms, 1776 to 581 ms, 118.5 to 50.5 ms. A 256 MiB file in memory: 1298 to 118 ms CPU ([raw](sha256-backend-329.json)). | The largest effect in `full` mode. arm64 SHA-2 instructions cause it. This ratio does not predict x86-64. |
| Policy | No effect in `full` mode | The difference is inside the spread. |
| Deduplication | `native-shared-library` 171.9 to 16.6 ms, with 33 to 2 operations. `process-shared-inputs` 617.6 to 432.1 ms, with spreads that do not meet (566.6 at the lowest, 471.8 at the highest), and 25 to 4 operations. `native-distinct-artifacts` no change. | Only repeated inputs gain. |
| Streaming | End to end 311.3 to 307.9 ms, with overlapping spreads. Execution increases by 44.0 ms, and finalization decreases by 47.0 ms. | **The hash work moved. There is no resolved end-to-end gain.** The reported Run wall increases by 43.8 ms because the timer now holds this work. |
| External | `native-distinct-artifacts` 346.6 to 12.0 ms. `native-recording-heavy` 307.9 to 261.7 ms. `native-shared-library` 16.1 to 8.0 ms. `replay-verification` 50.6 to 42.6 ms. `process-shared-inputs` 455.8 to 425.0 ms, with overlapping spreads, because Python start dominates it. | It removes real work, but the platform has this work if it needs the digests. A wrapper that hashes the same files gains nothing. |

## Attribution of the profile command

[`tools/profile_cost_measure.py`](../../tools/profile_cost_measure.py) measures
each command with a count of each hash of the profile tool. The end of the runner
is the modification time of the side-car.

Workload: `native-recording-heavy` under the fake perf, which **does not sample**.
Its export is small, so the post-Run time here is far below that of real perf.
Compare the two revisions, and do not take these as the cost of a real capture.

Median in ms. A hash column gives operations, then MiB.

| Case | Revision, mode | End to end | Capture and Run | Post-Run | Profile hash | Profile hash work | Runner digests | Runner Recording state |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| CPU | Base, default | 1010.7 | 793.6 | 213.6 | 37.2 | 4, 72.4 | 2 | `calculated` (schema 1) |
| CPU | `693594b`, `full` | 668.3 | 459.3 | 211.1 | 37.1 | 4, 72.4 | 2 | `calculated` |
| CPU | Tip, `full` | 666.9 | 456.1 | 209.1 | 37.2 | 4, 72.4 | 2 | `calculated` |
| CPU | Tip, `external` | 609.1 | 407.5 | 202.1 | 37.0 | 4, 72.4 | 0 | `omitted` |
| CPU, Activations | Base, default | 4475.9 | 3391.4 | 1071.9 | 137.7 | **10, 272.8** | 2 | `calculated` (schema 1) |
| CPU, Activations | `693594b`, `full` | 4117.2 | 3059.0 | 1051.9 | 137.0 | **10, 272.8** | 2 | `calculated` |
| CPU, Activations | Tip, `full` | 4038.1 | 3044.6 | 993.5 | 68.3 | **5, 136.4** | 2 | `calculated` |
| CPU, Activations | Tip, `external` | 3998.5 | 2999.1 | 996.7 | 68.3 | **5, 136.4** | 0 | `omitted` |

Full tables: [base](profile-cost-329-base-results.md),
[before the inventory change](profile-cost-329-before-inventory-results.md),
[tip](profile-cost-329-tip-results.md).

- **Runner saving.** The Capture and Run column shows it: 793.6 to 456.1 ms from
  the backend, and 456.1 to 407.5 ms from `external`. The profile hash work stays
  the same in `full` and `external`. This is **not a zero-hash mode**: 72.4 MiB
  are still hashed as profile evidence.
- **Finalization saving.** With Activations, the operations fall from 10 to 5 and
  the hash time from about 137 to 68 ms, because the inventory is built once. The
  remaining post-Run time, about 925 ms, is other processing, and these tickets
  did not change it. Only this hash row is a saving of finalization: the other
  difference is inside or near the spread.
- **The Activation series does not show a correlation.** The fake perf cannot
  prove a join, so this host writes no correlation file. Thus the Activations
  row has 5 files. `test_activations_hash_each_finalized_artifact_once` proves
  the count with the correlation file. The real join ran on CI
  ([profile-cpu-integration](https://github.com/Stevie1704/sil/actions/runs/41187537099)),
  but CI did not measure it.

### Real Heaptrack

Heaptrack 1.4.0 in a Debian arm64 container on this machine, `--scale 0.1`, 5
repetitions: [tables](profile-cost-329-linux-arm64-heap-results.md),
[raw](profile-cost-329-linux-arm64-heap.json). Median in ms. This is not the
supported machine class, and it ran in the Colima VM.

| Case | Mode | End to end | Capture and Run | Post-Run | Profile hash | Profile hash work | Runner digests |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Heap, runner | `full` | 1846.4 | 903.4 | 949.4 | 12.6 | 5, 12.0 | 2 |
| Heap, runner | `external` | 1795.1 | 827.7 | 969.6 | 12.9 | 5, 12.0 | 0 |
| Heap, Process | `full` | 1163.3 | 731.0 | 430.0 | 5.2 | 5, 3.7 | 3 |
| Heap, Process | `external` | 1154.6 | 722.0 | 431.6 | 4.9 | 5, 3.7 | 0 |

There is no before series for heap: the build in that container has no history.
A heap capture before these changes also built its inventory once.

## Consumer audit

### Readers of the side-car

| Consumer | Use | Result |
| --- | --- | --- |
| `python/src/sil/visualization/source.py` | Binds a Run outcome to the source through a digest | Reads schema 1 and 2. Only a `calculated` digest binds. All other states and an unknown schema give `unknown`. `tests/test_visualization_export.py`. |
| `tools/hash_baseline.py` | Checks each digest | An entry without a digest string makes no claim. The Recording digest of a schema 1 or 2 record is checked. |
| `tools/profile_cost_measure.py` | Reads the states | Schema 1 and 2 |
| `proofs/libsafety/acceptance.py`, `proofs/libsafety/native_acceptance.py`, `proofs/openacc-controller/acceptance.py` | Copy the whole record into evidence | No member is read. These run in `full` mode, as the default. Proof runs pass on CI. |
| `proofs/osmp-qualification/run.py`, `proofs/fmi2-importer/qualify.py`, `proofs/osmp-importer/prove.py`, `proofs/profiling-qualification/qualify.py` | Know the file name only | No member is read. |
| `python/src/sil/bundle.py`, `models/can/tests/*` | Set the path of the side-car | No member is read. A bundle calculates its own Recording digest. |
| `proofs/fmi-ls-bus/replay_provenance.py` | Writes its own record for an older pinned runner | Not a reader of the side-car |

A retained v1 side-car stays readable where a reader exists. Only the
visualization reader needs a digest from the side-car. It reports `unknown`
instead of a false outcome if the digest is not `calculated`.

### Report schemas

| Report | Without the member | Evidence |
| --- | --- | --- |
| `sil_run_diagnostics`, `sil_benchmark_report` | Valid, and reads as `full` | `test_a_diagnostics_report_before_the_option_stays_valid_and_reads_as_full`, `test_a_benchmark_report_before_the_option_stays_valid` |
| `sil_profile_report` | Valid, and reads as `full`; null reads as `unknown` | `test_render_says_that_a_legacy_set_is_treated_as_full`, `test_diff_treats_a_legacy_set_as_full`, `test_render_shows_the_policy`, `test_diff_flags_differing_policies` |
| Unknown value | Rejected | `test_a_report_with_an_unknown_policy_value_is_rejected`, `test_a_diagnostics_report_with_an_unknown_policy_is_rejected` |

### Other correctness hashes

`git diff 600fa429 67fee44` is empty for each of these modules. So this delivery
disables none of their hashes.

| Module | Hash |
| --- | --- |
| `python/src/sil/check.py` | `sil check` |
| `python/src/sil/bundle.py` | `sil bundle seal`, `sil bundle verify` |
| `python/src/sil/compare.py` | Comparisons |
| `python/src/sil/manifest.py`, `python/src/sil/manifest_validate.py` | Recorded input hash at authoring and validation |
| `python/src/sil/profile_set.py` | The check of a profile set |
| `python/src/sil/recording_query.py` | The source identity of a query |
| `python/src/sil/matrix.py` | Matrix results |

In the kernel, Replay verification is on in both modes
(`test_replay_hash_mismatch_is_rejected_in_both_modes`).

## CI

The evidence is draft PR #363 at `67fee44`, on x86-64 Linux. New tests of this
ticket are **not** in this run. They passed on arm64 macOS and, for the
Linux-only tests, in a Debian arm64 container ([limitations](#limitations)).

| Check | Run | Result |
| --- | --- | --- |
| `ci`: C++ tests, pytest, determinism gate, staged install, runtime and acceptance images, `test_container.py` | [41187537139](https://github.com/Stevie1704/sil/actions/runs/41187537139) | Pass. pytest: 3552 passed, 48 skipped. Container: 10 passed. The first run of `test_a_heap_capture_hashes_each_artifact_once` and of the heap policy tests passed here. |
| `profile-cpu-integration`, real perf | [41187537099](https://github.com/Stevie1704/sil/actions/runs/41187537099) | Pass |
| `profile-heap-integration`, real Heaptrack | [41187537050](https://github.com/Stevie1704/sil/actions/runs/41187537050) | Pass |
| `release`, release shapes | [41187537248](https://github.com/Stevie1704/sil/actions/runs/41187537248) | Pass |
| Selective image builds: `proof-profiling` [41187537146](https://github.com/Stevie1704/sil/actions/runs/41187537146), `proof-profiling-qualification` [41187537127](https://github.com/Stevie1704/sil/actions/runs/41187537127), `proof-adas-cost` [41187537096](https://github.com/Stevie1704/sil/actions/runs/41187537096), `proof-adas-closed-loop` [41187537025](https://github.com/Stevie1704/sil/actions/runs/41187537025), `proof-adas-equivalence` [41187537054](https://github.com/Stevie1704/sil/actions/runs/41187537054), `proof-osmp-qualification` [41187537125](https://github.com/Stevie1704/sil/actions/runs/41187537125), `can-model` [41187537083](https://github.com/Stevie1704/sil/actions/runs/41187537083) | As listed | Pass |
| `proof-acc-fmi` [41187537326](https://github.com/Stevie1704/sil/actions/runs/41187537326), `proof-osmp-importer` [41187537068](https://github.com/Stevie1704/sil/actions/runs/41187537068), `proof-fmi2-importer` [41187537176](https://github.com/Stevie1704/sil/actions/runs/41187537176) | Also red on `main` at `600fa429`: 41186969890, 41186972021, 41186973937 | **Existing failure, out of scope.** The errors are equal: `sensing: timestamp 4010000000 expected 4000000000` and `left ['out.mcap.failure.json']`. |

All other checks on this PR passed.

## Limitations

| # | Limitation | Consequence | What resolves it |
| --- | --- | --- | --- |
| L1 | **No x86-64 timing.** All timings are from arm64 (macOS, and one Linux container). #325 has this gap too. | The backend ratio and the absolute values do not describe the supported machine class. Correctness on x86-64 is proven by CI, not cost. | `.github/workflows/provenance-qualification.yml` is new. It runs after the next push, and it keeps the same captures with the real collectors as an artifact. A shared runner is not a quiet machine, and it does not gate anything. |
| L2 | **The tests that this ticket adds have not run on x86-64.** | They ran on arm64 only until the next push. | The `ci` job runs them on the next push. |
| L3 | **Profile timings use a fake perf.** | They show the runner and the finalization, not perf overhead or a real export. | The new workflow, `--real-tools`. |
| L4 | **No cold-cache series.** | A gain from no reread where a read is slow is not measured or claimed. | Needs administrator rights. Not planned. |
| L5 | **No Recording digest handoff.** The spec allows this fallback ([why](../profile-report.md#no-recording-digest-handoff)). | One bounded-memory hash remains in each capture with a Recording in the directory. | Nothing is needed. Tests compare the result with independent hashes. |
| L6 | **Load was not zero.** | The 1-minute load average was 2.8 to 7.0. The spreads are narrow except in `process-shared-inputs`. | Do not read small differences. |
| L7 | **Heap tests use a fake Heaptrack for policy variants.** | The real tool runs in `profile-heap-integration` and in the arm64 series. It does not run each option combination. | Nothing is needed for this contract. |
| L8 | **Local image builds did not run.** The Colima Docker disk is 100% full, and this work deleted nothing there. | Image checks come from CI. An existing image ran the Linux-only tests, with all files on a bound host directory. | CI |
| L9 | **Three proofs are red on this PR and on `main`.** | They do not qualify this work, and this work does not fix them. | Separate work |
| L10 | `test_the_digest_keeps_memory_bounded_as_the_file_grows` asserts on an observational RSS value. This work does not touch it. | Against the repository rule. | The code-review phase |

## Acceptance map

P = passing evidence. L = passing evidence with the named limitation. N = not met.

### Parent #323

| Item | State | Evidence |
| --- | --- | --- |
| Default and explicit full mode keep artifact coverage; digests match an independent implementation | P | `test_full_mode_digests_every_artifact_and_the_recording`, `tests/test_sha256.py` |
| External mode: paths with explicit omitted states, Manifest identity, no provenance-only hashing | P | `test_external_mode_keeps_paths_and_marks_every_digest_omitted`, `test_external_mode_performs_no_provenance_hashing`, External column above |
| Native, Process and mixed Runs byte-identical between modes; failures and invalid configuration keep their outcome | P | `tests/test_provenance_qualification.py`, `test_a_missing_artifact_is_unresolved_in_both_modes`, `test_a_failed_cpu_capture_run_keeps_its_exit_code` |
| Replay mismatch fails in both modes | P | `test_replay_hash_mismatch_is_rejected_in_both_modes`, the `malformed_replay` tests (appended, truncated, flipped, removed) for each command |
| One hash per canonical input path per preparation, with symlink aliases; a later Run sees replaced bytes | P | `tests/provenance_digest_test.cpp`, `test_shared_inputs_count_real_hashes_only_and_none_when_external`, `test_replacing_a_library_changes_only_the_full_mode_digest` |
| Output digest matches the finalized file: empty, large, failed partial | P | `test_the_digest_is_the_digest_of_the_finalized_file`, `test_a_failed_run_has_the_digest_of_its_partial_recording`, `test_a_failed_write_fails_the_run_without_a_false_digest` |
| No post-close scan in success; no output digest work in external | P | `test_a_successful_run_does_not_read_the_recording_again`, `test_external_mode_accumulates_no_output_digest`, Output pass row above |
| `--no-recording`, custom paths, diagnostics, benchmark, invalid options, schema states, v1 consumers | P | `tests/test_provenance_mode.py`, `tests/test_provenance_mode_cli.py`, [consumer audit](#consumer-audit) |
| Retained before/after benchmarks for the three workloads, with phases, bytes, cache, machine and distributions | L: L1, L4, L6 | [Attribution](#attribution-of-the-run). Cold cache is stated as not measured. |
| CPU with and without Activations and heap for runner and Process forward each policy and record it; Recording and outcome equal; invalid and repeated options, no Recording, custom paths, failed Runs and Replay mismatches | P on arm64 Linux and macOS, L2 for x86-64 | `tests/test_provenance_mode_cli.py`, `tests/test_provenance_qualification.py`. The heap tests for no Recording, custom paths, failed Run and Replay mismatch are new, as #327 did not have them. |
| Render and diff show policy and unknown; v1 sets readable; tampered evidence rejected in both modes | P | `test_render_shows_the_policy`, `test_diff_flags_differing_policies`, `test_a_profile_rejects_tampered_normalized_evidence_in_each_mode`, `test_heap_profile_rejects_tampered_normalized_evidence` |
| Activations cause no second hash; handoff or fallback and external digests match independent results, with failure cases | L: L5 | `test_activations_hash_each_finalized_artifact_once`, `test_full_mode_hashes_the_recording_apart_from_the_runner`, `test_external_mode_keeps_the_profile_digest_of_the_recording`, `test_a_failed_run_keeps_the_digest_of_its_partial_recording`. The handoff is not built: the fallback path applies. |
| Profile qualification keeps runner and profile hashing apart, with before and after end-to-end values; no zero-total claim | L: L3 | [Attribution of the profile command](#attribution-of-the-profile-command) |
| Docs: CLI, Manifest bytes, platform duties, Replay exception, migration, profile separation, backend dependencies | P | `docs/running.md`, `SUPPORT.md`, `docs/profile-report.md`, `docs/releases/unreleased.md`, `docs/adr/0006-sha256-backend-static-openssl.md` |
| `make build`, `make test`, determinism gate, installed and container checks | P for this branch on macOS; L2 for CI on this branch | The final branch results are in the handback. CI at `67fee44`: [ci](https://github.com/Stevie1704/sil/actions/runs/41187537139). |

### Ticket #329

| Item | State | Evidence |
| --- | --- | --- |
| All items of the parent are mapped | P | The table above |
| Across policies, equal bytes; invalid Replay digests fail | P | `tests/test_provenance_qualification.py` |
| Work counts: input reuse, no post-close scan, no provenance-only hash in external | P | As mapped above |
| Retained results with limitations and no flaky timing gates | L: L1, L3, L4 | This file. The tool tests read no duration. |
| v1 reader, full-hash consumers, failure reporting, mode-specific v2 states | P | [Consumer audit](#consumer-audit), `test_a_failed_recording_digest_is_not_an_omitted_one` |
| Build, test, gate, and installed and container checks | L: L2, L8 | CI table, final results in the handback |
| CPU and heap workflow evidence for both policies and scopes; malformed Replay rejected | P on arm64, L2 for x86-64 | `tests/test_provenance_qualification.py`, Heaptrack table |
| Render and diff; tampering in both modes; no duplicate finalization hash; handoff or fallback digests | L: L5 | As mapped above |
| Profile before and after results | L: L1, L3 | Profile tables |
| Parent tracker updated with evidence links | **N** | By instruction, this work does not edit issues. The owner links this report. |
| Managed-platform example (scope) | P | `examples/managed-platform/`, `tests/test_managed_platform_example.py`, [docs](../running.md#managed-platform-example) |
