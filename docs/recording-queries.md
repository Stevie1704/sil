# Bounded Recording queries

`sil recording query` inspects selected fields at full resolution and returns
one bounded JSON report. Use it to locate a scalar match, summarize a field,
then read nearby inputs. Times are **publication times**, in nanoseconds.
A payload's Sample time or age field retains the Participant's own contract;
this command does not derive Observation age from publication time.

```sh
sil recording query failed.mcap --channel cadence.command \
  --field radar_age_ns --field mode \
  --where radar_age_ns cross-up 40000000 --max-results 1 --max-bytes 4096
sil recording query failed.mcap --channel cadence.radar \
  --field sample_time_ns --field 'object_id[0]' --field 'x_m[0]' \
  --from 80000000 --to 120000000
sil recording query failed.mcap --channel cadence.command \
  --field radar_age_ns --summary
```

The [retained ADAS regression](../proofs/recording-query/README.md) crosses
this limit at 90 ms. Its nearby radar inputs include a Message published at
110 ms whose payload Sample time is 60 ms. A query reports these facts; it
does not establish their cause. `show --json` retains its existing per-Message
format. `query` always prints JSON, including refusals.

## Selection and predicates

Select exactly one declared Channel with `--channel`, and one or more fields
with repeatable `--field`. A scalar field, a whole array, and an explicit
element such as `x_m[0]` can be projected. Indices are zero-based; invalid
fields, duplicate selectors, invalid Schemas and out-of-bounds indices are
refused. Whole `u8` arrays become lowercase hex strings; other arrays become
lists. Non-finite floats become `"NaN"`, `"Infinity"` or `"-Infinity"`.
Exact declared names take precedence over array suffix parsing, including
names containing dots, spaces, Unicode or brackets. If a literal field name
such as `values[1]` exists, that selector names the field rather than an element
of a separate `values` array.

`--from T` includes T and `--to T` excludes T. Either bound may be omitted.
Bounds must be non-negative integer nanoseconds, with end at least start.
Equal bounds, a declared Channel without Messages, or a predicate without
matches are completed empty selections. No selected fields is a refusal.

`--where FIELD OP JSON_NUMBER` supports only these operators on one scalar
field or explicit array element:

| OP | Match |
| --- | --- |
| `eq`, `ne`, `lt`, `le`, `gt`, `ge` | current finite value compares with the literal |
| `cross-up` | previous finite value <= literal, current finite value > literal |
| `cross-down` | previous finite value >= literal, current finite value < literal |

Equality belongs to the starting side of a crossing. The first selected
Message never crosses. No predecessor outside the requested interval is read;
`crossing_context` discloses this. A non-finite value never matches, even for
`ne`, and breaks crossing adjacency. `scan.predicate_nonfinite` counts these
values. A missing field is a refusal; a Message with an incorrect payload
size refuses the query, including after an output limit has been reached.

Literals must be finite JSON numbers. An integer field requires an integer
literal within that wire type's range, preserving UInt64 precision. Float
fields accept integer or float literals; comparison uses the decoded value
without rounding the literal to f32. Booleans, strings, arrays, expressions
and compound predicates are refused. There is no cross-Channel predicate.

Rows keep stored Publish order, including equal-time Messages and Bursts.
Crossing queries require nondecreasing publication times **within the selected
Channel and interval**. An encountered regression is a refusal. The first
returned crossing is therefore the earliest inspected crossing. Stored order
breaks equal-time ties. Rows and summaries also support unsorted times.

## Summaries

`--summary` returns one result per selected scalar or explicit array element.
Whole arrays are refused in this mode. A predicate, if present, filters the
Messages summarized. Each result contains `count`, `finite_count`,
`nonfinite_count`, `min` and `max`. Extrema consider finite values only and
carry the exact value and evidence locator. Equal extrema choose the earliest
publication time, then the smallest Message index. Empty or exclusively
non-finite selections have null extrema. A partial scan's summaries cover
only the inspected Messages. No downsampling is used.

## Bounds, completion and evidence

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-results N` | 100 | maximum returned rows, or summary field results |
| `--max-bytes N` | 65536 | maximum canonical UTF-8 report bytes, including metadata and newline; minimum 1024 |
| `--max-messages N` | unlimited | maximum selected Messages inspected |

Result and byte limits retain a prefix and **continue the exact scan**. Counts
and `scan.complete` cover that scan, even if no results fit. `output.omitted`
counts omitted matching rows (or summary field results). `first_omitted`
locates the first omitted result and gives `max_results`, `max_bytes` or
`oversized_result` as its reason. Once a result cannot fit, later results are
also omitted. A single oversized array has a limited report with an explicit
omission; its full decoded array is not constructed. Space is reserved for
completion metadata, so results can stop before the absolute byte ceiling.
A budget too small for the query's metadata is a refusal.

The scan limit stops inspection and produces `incomplete`. The reader may
fetch **one extra Message**, without decoding its selected fields, to tell
exhaustion from a stopped scan; `lookahead_messages` discloses this. Exactly N
Messages in a selection with a limit of N is a complete scan. This limit does
not bound source hashing, index loading, chunk decoding or wall-clock time.

| Exit | `status` | Meaning |
| --- | --- | --- |
| 0 | `complete` | scan completed and all results returned |
| 2 | `refused` | invalid request, unreadable/changed source, or invalid selected data |
| 3 | `incomplete` | inspection stopped at the scan limit; results or summaries are partial |
| 4 | `limited` | scan completed, with some results omitted by output limits |

Only a completed exact predicate scan with `scan.matches == 0` establishes
that the predicate had no matches in the selection. Empty returned results
alone do not. A summary alone does not establish that no crossing occurred.
An incomplete scan can report a found match but cannot exclude later matches
or a later publication-time regression.

The report is **`sil_recording_query` version 1**:

- `source`: Recording SHA-256 and `size_bytes`. Every result inherits it.
- `query`: Channel, field selectors, mode, publication-time bounds and typed
  predicate (or null). `query_sha256` hashes UTF-8 of `render(query)`.
- `limits`: the three requested budgets; these are separate from query identity.
- `scan`: completion, selected Messages inspected, matches, predicate non-finite
  count, minimum/maximum inspected publication times, last inspected Message
  index, and lookahead count. Times and last index are null for an empty scan.
  `inspected_message_range` is the half-open range of inspected Message indices,
  starting at zero. `first_message` and `last_message` locate its endpoints in
  stored order; `next_message` locates the fetched, uninspected Message when the
  scan limit stops inspection. Each position has Channel, publication time and
  Message index. All three locators are null for an empty scan.
- `results`: projected rows with `channel`, `t_ns`, `message_index`, and `fields`,
  or summary field results. Every predicate row also carries `match`, with the
  predicate field's value and locator, even when that field is not projected.
  Crossing rows also carry the `previous` Message's field value and locator.
  Extrema locators contain Channel, field, time, index and value.
- `output`: completion of returned results, returned/omitted counts, first
  omission and the exact `serialized_bytes` of the canonical report.
- `crossing_context`: outside-window and `first_message`/non-finite policies, or null.
- `continuation`: always null in version 1. Pagination is not supported.

`message_index` is the zero-based position in the **selected Channel/time
stream before applying a predicate**. It distinguishes same-time Messages
without retaining a timestamp table. Cite it together with the source digest,
Channel, field, publication time **and query interval**. A narrower interval
can change the index. Changing only fields, predicates or result budgets
does not change indices for the same Channel and interval.

For unsorted publication times, `first_t_ns` and `last_t_ns` are extrema of
the inspected Messages. They do not describe a fully inspected time interval.
For example, an incomplete scan can inspect times 30 and 10, then stop before
times 20 and 15. Its extrema are 10 and 30, but its inspected Message range is
`[0, 2)`. The position locators identify exactly what was read and what is next.

Refusal reports contain status, source identity when available, null scan,
empty results, bounded error text and `request_sha256` of `render(arguments)`
for the raw command argument list. `arguments` contains that list when it
fits, otherwise null. Output size is also reported. A malformed CLI syntax
uses the default byte budget; an invalid byte budget cannot be honored and
is refused using the default. Error text is not a stable contract.

## Python API and scan cost

```python
from sil.recording_query import Predicate, RecordingQuery, render

source = RecordingQuery("failed.mcap")
crossing = source.query("cadence.command", ["radar_age_ns", "mode"],
                     where=Predicate("radar_age_ns", "cross-up", 40_000_000),
                     max_results=1, max_bytes=4096)
nearby = source.query("cadence.radar", ["sample_time_ns", "x_m[0]"],
                      start_ns=80_000_000, end_ns=120_000_000)
print(render(crossing), end="")
```

`query()` accepts `mode="rows"` or `"summary"`, `start_ns`, `end_ns`, `where`,
`max_results`, `max_bytes` and `max_messages`. It returns the versioned report
or raises `QueryError`. `render()` defines byte-budget serialization: compact
ASCII-escaped JSON, finite JSON numbers only, and one trailing newline.

Constructing a `RecordingQuery` object reads the entire file once for SHA-256,
in 1 MiB blocks: O(file bytes). Queries on the same object reuse that digest.
Each independent CLI invocation constructs a new object and hashes again;
there are no pages or persistent hash cache. Device, inode, size, mtime and
ctime are checked before and after scans, refusing an observed source change.
Use completed immutable Recordings; these checks are not a snapshot against
adversarial concurrent filesystem replacement.

Each scan streams through `sil.recording.read_records`. The reader loads
summary/index metadata and sorts chunk indices by their physical file offsets;
this state grows with chunk count. It parses indexed chunks one at a time,
materializing chunk contents and decoded records; a large individual Message
also sets a memory floor. Without chunk indices it scans sequentially. Channel and time
filters can skip indexed chunks, but every Message in a selected chunk must
still be parsed. Selected scalar decoding is proportional to selected fields;
summaries retain counters and two extrema per field, predicates one previous
scalar, and returned rows are bounded by result/byte budgets. Array projection
can allocate temporary lists proportional to the output budget. There is no
constant-RSS claim independent of Recording length or chunk layout.

The [acceptance evidence](../proofs/recording-query/README.md) retains measured
peak RSS and output bytes for two Recording lengths and two chunk sizes,
including a one-Message threshold violation missed by an every-100th-Message view.
