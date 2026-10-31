# Recording digest cost

This capture shows where the cost of the Recording digest goes after #328. The
runner now calculates the digest while it writes, and it does not read the
Recording again. It is evidence, not a threshold: no test or gate reads a value
from it.

**The machine was not quiet.** Other coding agents built and tested on it (the
1-minute load average went from about 3 to about 18). Do not use these values to
compare small differences. [The qualification](provenance-qualification.md) repeats the capture under
less load, with the same result: the hash work moved, with no resolved end-to-end
gain in `full` mode.

The machine is an Apple M1 Pro with macOS on arm64, with a warm page cache. This
is not the supported Linux x86-64 machine class. The capture has no cold-cache
series, so it does not show the cost of the read from a slow disk.

## Procedure

`tools/hash_baseline.py`, workload `native-recording-heavy` (1.2 million
Messages, a 72 MiB Recording), 9 repetitions, OpenSSL backend.
[hash-baseline.md](hash-baseline.md) gives the timing boundaries.

| Series | Build | Mode | Tables | Raw |
| --- | --- | --- | --- | --- |
| Before | `16c2683`, which reads the file again | `full` | [results](hash-baseline-328-before-results.md) | [JSON](hash-baseline-328-before.json) |
| After, full | `809b4b1` | `full` | [results](hash-baseline-328-after-full-results.md) | [JSON](hash-baseline-328-after-full.json) |
| After, external | `809b4b1` | `external` | [results](hash-baseline-328-after-external-results.md) | [JSON](hash-baseline-328-after-external.json) |

The after series state "uncommitted changes". The only such changes were the
evidence files of the earlier series.

## Results

Medians in ms.

| Value | Before | After, full | After, external |
| --- | --- | --- | --- |
| Ordinary end to end | 321.2 | 323.4 | 266.9 |
| Preparation | 4.7 | 4.5 | 0.9 |
| Execution and Recording | 301.4 | 348.9 | 296.8 |
| Finalization | 49.7 | 1.7 | 1.2 |
| Output hash pass after close | 48.5 | 0 | 0 |
| Output hash operations | 1 | 0 | 0 |
| Reported Run wall | 309.1 | 355.2 | 299.0 |

## What this shows

- **The reread is gone.** The output hash pass of 48.5 ms has no equal in the
  after series, and a work count in `tests/test_recording_digest.py` proves it.
- **The hash cost moved. It did not go away.** Execution and Recording increases
  by about 47.5 ms, and finalization decreases by about 48 ms. `full` minus
  `external` gives the hash cost while the Run writes: about 52 ms.
- **`full` mode has no measured end-to-end gain on this workload.** 321.2 ms and
  323.4 ms are equal within this noise. With a warm cache, the read of the file
  costs little, and most of the old pass was SHA-256 CPU work. A gain can occur
  where a read is expensive, but this capture does not measure that case.
- **The reported Run wall increases.** It now includes the digest. This is a
  change of what the timer holds, not a slower Run. Compare the end-to-end time.
- **`external` mode removes the cost.** It saves about 54 ms end to end, which
  includes the input hashes in preparation.

## Peak memory

`kernel_max_rss_bytes` of `sil-run-instrumented`, 3 Runs for each row, all equal:

| Recording | `full` | `external` |
| --- | --- | --- |
| 72 MiB | 6 MiB | 6 MiB |
| 288 MiB | 6 MiB | 6 MiB |

The peak does not change with the file length or the mode.
`tests/test_recording_digest.py` keeps a check with a wide margin.
