# Retained Linux evidence

Captured by [Actions run 41927490239](https://github.com/Stevie1704/sil/actions/runs/41927490239)
at commit `88231d293b42dc2eaecc71bd2e6017da66dea4f7` (branch
`feat/372-scope-capture`) on ubuntu-24.04: Linux 7.0.0-1017-azure x86-64,
glibc 2.39, GCC 12.4.0 (`12.4.0-2ubuntu1~24.04.1`), binutils 2.42, perf
7.0.14 (`host.txt`).

| File | Content |
| --- | --- |
| `host.txt` | Host, compiler, binutils, glibc and perf identities |
| `build-identities.txt` | SHA-256 of `sil-run`, `sil-run-diagnostics`, the launcher, `libsil_source_scope.so.1` and the scenario libraries; build IDs; the observer's exported symbols (only `sil_source_scope_v1_*`) |
| `tests.txt` | The verbose pytest log of the source-scope tests |
| `results.json` | The ADAS proof: build commands and identities of both builds, 12 Runs, Recording SHA-256 |
| `original.inline.txt`, `instrumented.inline.txt` | GCC 12 inline decisions of both ADAS builds (`-fopt-info-inline-optimized`) |
| `capture-one-0/`, `capture-eight-0/` | `scopes.json`, `scopes/map.json` and `scopes/calls.json` of the first capture of each maneuver |

The workflow artifact `source-scopes-evidence` (30 days) also holds the raw
`events.bin` files, observation streams, Recordings, copied sources and
libraries of every Run.

## Rows

| Row | Evidence | Result |
| --- | --- | --- |
| Q1 | `results.json` captures, `capture-*-0/` | Every one of the 20 Activations of each maneuver is `exact` with counts (2, 1, 1, 1) and (16, 8, 36, 1) and #320's entry order, in all 6 captures |
| Q2 | `test_recursion_nests_three_calls` | 3 nested calls, depths 0–2 |
| Q3 | `test_an_early_return_is_a_return_event` | 30 entries, 30 returns; both paths of scope 4 return |
| Q4 | `test_two_tasks_at_one_virtual_time_keep_their_calls` | Tasks at equal Virtual times keep {5: 1} and {6: 2} |
| Q5 | `test_longjmp_out_of_two_scopes_is_unbalanced` | `partial`, `unbalanced_scopes`; 2 `missing_return` rows per Activation (scopes 7, 8) |
| Q6 | `test_an_exception_through_a_scope_has_no_return` | 10 entries, 0 returns; `unbalanced_scopes` |
| Q7 | `test_a_worker_thread_scope_is_unattributed` | `worker_thread` rows; runner Activations `exact` |
| Q8 | `test_calls_of_a_failed_or_interrupted_activation_are_withheld` | Activation 2 `failed` / `interrupted`: `not_completed`, 1 call withheld |
| Q9 | `test_lost_events_make_the_capture_partial` | 7 records: `dropped_full`; contention: `dropped_contended`; `events_lost`, `partial`, Recording equal to `sil-run` |
| Q10 | `test_an_observer_that_cannot_activate_is_unavailable` | Size, header (capture ID) and `mmap` failure: state `unavailable`, decode `observer_unavailable`, exit code and Recording equal to `sil-run` |
| Q13 | `test_an_existing_output_directory_is_never_replaced`, `test_output_problems_and_raw_failures` | Exit 2, nothing replaced; `posix_fallocate` failure (EFBIG): exit 1 before the Run; corrupt header: `raw_invalid` |
| Q14 | `test_a_killed_runner_keeps_its_records`, `test_an_interrupted_command_keeps_the_run_outcome` | Exit 137, `raw_incomplete`, 7 kept records in state `active`; SIGINT: `interrupted`, Run outcome `interrupted` kept |
| Q15 | `test_finalization_is_one_store`, `test_the_library_is_finalized_before_the_run_ends`, `test_the_runner_unloads_the_library_before_it_exits` | The observer's `finalize` has no call, jump out or system call; state `finalized`; shutdown phases and the end line written; Recording equal; the observer is unloaded by `dlclose` (order: see below) |
| Q16 | `test_a_full_file_system_keeps_the_raw_records` | All 4,000 raw records kept on a full tmpfs; `observations_incomplete`; Run succeeded |
| Q17 | `test_the_library_is_dormant_outside_a_scopes_capture`, `test_the_library_is_dormant_in_a_cpu_profile`, `test_a_process_that_is_not_the_runner_stays_dormant` | `sil-run`, `sil run`, the diagnostics runner, a CPU profile and a Process participant: equal Recordings and exit codes; a dormant observer leaves the raw file byte-identical |
| Q18 | `test_ordinary_runner_sources_have_no_scope_observer`, `test_ordinary_runner_binary_has_no_scope_observer` | No `sil_source_scope` in the kernel sources or the `sil-run` binary |
| Q19 | `test_without_the_opt_in_*`, `test_with_statement_markers_only_the_line_table_differs` | Same bytes with `-g -gno-statement-frontiers` and without `-g`; no `DT_NEEDED`. With default `-O2 -g`, only `.debug_line` and the build ID differ (see below) |
| Q20 | `results.json` `builds`, `*.inline.txt`, `capture-*-0/scopes.json` `identities` | Both builds' SHA-256, build ID, `DT_NEEDED` and inline decisions; GCC 12 inlined `confirms` and `confirmed` in both builds |
| Q21 | `results.json` `runs` | 3 repeats × (`sil-run` original, `sil-run` instrumented, 2 captures): 12 exit codes 0, one Recording SHA-256 `2c9532406de00e2d9cffb2207ce6798659b688a9f00b877e2c62e772f4c11334`; elapsed host time and storage per Run retained |

Q19 finding: GCC 12 at `-O2 -g` writes a line-table statement marker for
every statement, also for a scope line that declares nothing. The opt-out
build then differs from a build without the scope lines in `.debug_line`
only (and in the build ID, which hashes the file). Code, data, symbols and
dynamic section are the same.

Q15 order (code trace, `kernel/src/main.cpp` `run`): the `sil::Engine` is
a local of the inner `try` block. It owns each Native participant
(`Engine::natives_`), and `~NativeParticipant` calls `dlclose`
(`kernel/src/native_participant.cpp`). The block ends, so the library is
unloaded and the observer's destructor stores `finalized`, before
`recorder->close()` (phase `recording_finalization`) and before
`sil::diagnostics::end_run()`, which writes the observation `end` line
(`ObservationLog::close`). The test shows with `LD_DEBUG=files` that the
loader destroys the observer's link map, which it does only at a
`dlclose`.

Observed elapsed host time per Run (no threshold): `sil-run` about 7 ms; a
capture about 115 ms including the Python command, preflight and decode.
A capture directory is about 1.3 MiB, 1 MiB of it the preallocated raw file.
