# Run diagnostics overhead

One observational comparison of the ordinary runner, `sil-run`, with the
diagnostics runner, `sil-run-diagnostics`, for #299. It shows what turning
diagnostics on cost for three workloads on one named machine and one build.
It is evidence, not a threshold: no test asserts a duration from it, and no
value here is a correction to subtract from a diagnostics measurement.

Raw results are in [diagnostics-overhead.json](diagnostics-overhead.json) and
the tables in [diagnostics-overhead-results.md](diagnostics-overhead-results.md).
Both come from:

```sh
.venv/bin/python tools/diagnostics_overhead.py --build-dir build \
  --machine "<a name for the machine>"
```

## Machine and build

| Item | Value |
| --- | --- |
| Machine | MacBook Pro (`MacBookPro18,1`), Apple M1 Pro, 10 logical CPUs, 16 GiB, on mains power. A developer session that was not otherwise idle. |
| Operating system | macOS 27.0.1, arm64 |
| Build | SiL 0.1.0, revision `d2255cc8eda324bc580babb37eda682abbc377c6`, clean worktree, CMake `Release` |
| Report versions | `sil_run_diagnostics` 1, collector version 2 |

This machine is **not** the supported machine class. SUPPORT.md names Linux
x86-64. There is no x86-64 Linux result here. On Linux, values can differ, and
the Linux resource collector has limitations that macOS does not
(`peak_includes_parent_rss_at_spawn`).

## Method

- 20 pairs per workload. Each pair is one ordinary Run and then one
  diagnostics Run of the same Manifest, with the Recording on. Alternating the
  runners spreads slow drift of the machine over both.
- Both runners start through `sil-diagnostics-launcher`, so both pay the same
  launcher cost and no runner inherits the peak memory of the Python script.
- "Wall" is the host monotonic time from the start of the launcher process to
  its reap. It includes process start, Manifest load, the Run, the Recording
  close, the SHA-256 of the Recording for the provenance record, the
  diagnostics report write (diagnostics only) and process exit. It is wider
  than the report's Run wall time, which ends at the Recording close.
- "Largest-process peak RSS" is the `ru_maxrss` that `wait4` returns for the
  launcher and its waited descendants: the peak of the largest single process,
  never a sum.
- Every pair must write byte-identical Recordings; the tool stops otherwise.

## Results

| Workload | Activations | Ordinary wall, median | Diagnostics wall, median | Ratio | Report Run wall, median |
| --- | --- | --- | --- | --- | --- |
| Native, 2 Tasks (1 ms, 5 ms), 100 s Duration | 120,000 | 86.2 ms | 94.5 ms | 1.096 | 41.0 ms |
| the same, 1,000 s Duration | 1,200,000 | 665.2 ms | 699.6 ms | 1.052 | 303.9 ms |
| 2 Native Tasks and a Python Process participant, 60 s Duration | 9,000 Native, 6,000 Process Steps | 195.6 ms | 200.9 ms | 1.027 | 181.1 ms |

Minimum and maximum of each series are in the generated tables. All 60 pairs
wrote identical Recordings.

- **Collector time.** On this machine and build, diagnostics added about
  8 ms to the 100 s Native workload and about 34 ms to the 1,000 s one: about
  30 to 70 ns per Native activation, including the timer reads around each
  activation. For the Process workload, the added time (about 5 ms over 6,000
  Steps) is small next to the IPC cost of each Step. These values come from
  medians of noisy wall times; they are an order of magnitude, not a
  per-activation constant.
- **Bounded storage.** The collector keeps at most 8,192 duration samples and
  20 slowest activations per Task. With 10 times more activations (100 s to
  1,000 s), each Task still retained 8,192 samples, marked approximate, and
  the largest-process peak RSS of the diagnostics Run stayed at a median of
  5.3 MiB in both cases (4.4 MiB for the ordinary runner). The runner peak
  RSS in the report was 4.5 and 4.6 MiB. The added memory depends on the
  number of Tasks, not on the number of activations.
- **Outside the Run wall time.** For the 1,000 s workload, about 400 ms of the
  diagnostics wall time is outside the report's Run wall time. Most of it is
  the SHA-256 of the 1.2 million-Message Recording for the provenance
  record: in one spot check, not part of the retained series, an ordinary
  Run of the same Manifest took 0.12 s with `--no-recording` and 0.73 s
  with a Recording. That cost belongs to the Recording, not to
  diagnostics.

## Limitations

- One machine, one build, one session that was not otherwise idle. Do not
  compare these numbers with numbers from another machine.
- The diagnostics-runner timings include the collector's own cost. No guessed
  overhead is subtracted from any value, and these numbers do not predict the
  timing of an uninstrumented Run, a target ECU, or a capacity limit.
- A Run near a response deadline can miss it with diagnostics on, because the
  collector adds work to each activation. The Runs here had no such deadline.
- The [#229 reference execution-cost baseline](../decisions/229-reference-execution-cost.md)
  remains the reference for the ADAS workloads. This comparison does not
  change it and does not resolve [#125](https://github.com/Stevie1704/sil/issues/125).
