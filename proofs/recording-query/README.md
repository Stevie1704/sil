# Bounded Recording query acceptance (#282)

This proof starts with the [investigator trial](../agent-investigation/README.md)
and its retained `delay-regression` ADAS Run. The trial needed a full divergence
list/count per field; it did not need predicate search at its 20-activation scale.
This extension provides single-Recording projection and numeric summaries.
It does not replace a Comparison contract or implement paired divergences.
The original trial does not establish an investigator's need for every
operator. The comparison/crossing vocabulary comes from the requested scope of
#282; the checks below establish retrieval correctness on its retained inputs.
They do not claim a measured benefit for an agent investigation.

The first check finds a known threshold crossing in that actual Recording,
compares it with an independent binary decoder of the published ADAS C layout,
and reads nearby radar inputs:

- `cadence.command.radar_age_ns` changes from 40 ms at publication time 80 ms
  to 50 ms at 90 ms. The `cross-up 40000000` result is Message index 9, mode 2.
- In `[80 ms, 120 ms)`, `cadence.radar` contains a delayed Message published
  at 110 ms with Sample time 60 ms and a Message at 100 ms with Sample time
  100 ms, in that stored order. Both have object ID 30 and x = 50 m.
- Summary extrema agree with the direct command scan. The source digest is
  pinned to the retained trial's provenance, not derived from query output.

The proof checks all eight requested operators against worked Message indices
from that pinned trajectory. With a literal of 40 ms, `eq` matches index 8,
`gt` matches 9 and 10, `ge` matches 8 through 10, `lt` matches 17 Messages and
`le` matches 18. `ne` matches every index except 8. `cross-up` matches 9 and
`cross-down` matches 11. Match values and locations agree with the independent
binary scan, even when only `mode` is projected. Filtered summary counts agree
with those matches. This is a numeric retrieval check, not a freshness verdict.

To check exact predicate search on a long selection, the proof expands the
recorded command payloads into a **synthetic throughput fixture**: all Messages
use its normal age-0 payload except one age-50-ms payload from the known crossing.
Publication times increase by 10 ms. Payload Sample times remain the original
values. This fixture is not a new Run or a domain trajectory. It answers a
concrete retrieval question: locate that one excursion in a million Messages.
An every-100th-Message view misses it; a full-resolution predicate scan agrees
with a separate sequential MCAP reader and a literal-offset binary decoder.

```sh
SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk \
PYTHONPATH=python/src .venv/bin/python proofs/recording-query/verify.py \
  --out build/recording-query-proof
```

Omit `SDKROOT` on Linux. The script writes reproducible fixture Recordings,
cost receipts and `evidence.json` outside the tracked proof directory. It
uses the existing small C launcher from `proofs/adas-cost` so a Linux child's
peak RSS is not inflated by the Python driver's peak. The CLI scan, source
hashing and JSON emission run in a fresh process. The independent reference
scan and fixture generation are outside its measured cost. The source files,
seed Recording, machine class, Python and MCAP versions are identified in
the retained [evidence.json](evidence.json). These observations are macOS
arm64 measurements; Linux CI repeats the functional checks, not those RSS
numbers. The ordinary tests repeat the proof at 1,000 Messages per chunk size.

| Messages | Chunk target | Recording bytes | Chunks | Peak RSS | Output bytes |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 64 KiB | 1,033,364 | 14 | 28.2 MB | 1,431 |
| 1,000,000 | 64 KiB | 103,196,375 | 1,327 | 32.0 MB | 1,457 |
| 10,000 | 1 MiB | 1,031,453 | 1 | 31.8 MB | 1,431 |
| 1,000,000 | 1 MiB | 103,013,507 | 83 | 34.6 MB | 1,457 |

RSS includes interpreter/import overhead, hashing buffers, summary/index
metadata, decoded chunk records, selected scalar state and returned results.
The index grows with chunk count; larger chunks increase transient allocations.
The [query contract](../../docs/recording-queries.md#python-api-and-scan-cost)
states these costs separately. This is measured bounded payload processing,
not a constant-memory guarantee for arbitrary file layouts or large Messages.

The first measurement exposed an existing reader defect: MCAP 1.4's seeking
iterator with `log_time_order=False` queues all chunk indices, then all selected
Messages, before yielding its first Message. Peak RSS was 493.9/507.2 MB on the
two million-Message fixtures. [before-reader-fix.json](before-reader-fix.json)
retains those measurements. `read_records` now parses one indexed chunk at a
time in physical file order and retains Channel/time filtering. A public-reader
regression test detects whole-selection buffering before its first yield, and
cross-chunk tests cover equal times, unsorted times, unindexed and unchunked
Recordings. No Recording bytes, existing JSON streams or kernel contracts change.

The feature tests also exercise empty/non-finite selections, typed literals,
UInt64 precision, invalid fields and array indices, strict crossing direction,
first-Message/equality semantics, malformed data after an output limit, source
changes, bounded refusals, oversized arrays/bytes and scan versus output limits.
Run tests publish real Bursts over inline Transport and one- and two-slot
Arenas. They check same-Slot crossing witnesses and Publish order. Reader
filters also accept a `frozenset` of Channel names in indexed and unindexed
Recordings. No pagination or additional predicate operators are introduced.
