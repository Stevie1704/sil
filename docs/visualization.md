# Visualize a Recording in an external viewer

[Documentation index](README.md)

`sil recording export` converts a finalized Recording into an indexed MCAP
file that an external file player, such as Foxglove, opens directly. The
player supplies play, pause, seek, speed, loops and plots. SiL supplies no
viewer, extension or server on this path. To play a Recording to Foxglove
without an export, see [Serve a Recording to Foxglove](#serve-a-recording-to-foxglove).

The output is a **visualization derivative**. It is not a Replay input: the
kernel replays `sil_pod` Recordings only. The source Recording stays
unchanged and stays the authoritative byte-level evidence.

## Export

```sh
sil recording export run.mcap --profile visualization-json -o run.visualization.mcap
```

```python
from sil.visualization import export_recording

receipt = export_recording("run.mcap", "run.visualization.mcap",
                           profile="visualization-json")
```

- `--channel NAME` exports only that Channel. Repeat it for more Channels.
  Without it, every Channel is exported, also Channels without Messages.
- The command writes two files: the output MCAP and
  `<output>.receipt.json`. It refuses to overwrite either file.
- Exit codes: `0` success, `2` rejected input or configuration, `1` output or
  I/O failure. After a failure, neither file exists and no temporary file
  stays.

The source must be a finalized MCAP Recording with `sil_pod` Channels and
supported SiL Schemas. The command rejects truncated files, other encodings,
unsupported field types and payloads that do not match their Schema. A
Recording without Messages is a valid export; it has no playback range.

A failed Run also writes a finalized Recording. Its export is valid and shows
the Messages that the Run recorded. Message coverage does not prove that the
Run completed: see [Completeness](#completeness).

## What the output contains

The output is an MCAP file with chunks, Message and chunk indexes, a summary
and statistics, compressed with zstd. Each source Channel becomes one topic
with the same name. The profile is `sil_visualization_json` version 1:
`jsonschema` schemas and `json` Messages.

Each Message has two members. Source field names never collide with SiL's
own members:

```json
{
  "sil": {
    "publication_time_ns": "2000000000",
    "record_index": "412",
    "nonfinite": {"/fields/accel": "NaN"}
  },
  "fields": {"speed_mps": 12.5, "counter": "9007199254740993", "accel": null}
}
```

| SiL value | In the output |
| --- | --- |
| Integers up to 32 bits | JSON integer |
| `i64`, `u64`, also as array elements | Decimal string |
| Finite `f32`, `f64` | JSON number with the decoded value |
| Non-finite float | `null`, and a JSON pointer in `sil.nonfinite` names `NaN`, `Infinity` or `-Infinity` |
| `u8` array | Base64 string |
| Other arrays | JSON array of the declared length |

Decimal strings keep all 64 bits in JavaScript viewers. Inspect them as
exact values; do not expect them as plot series. Missing data never becomes
zero.

The generated schema of each topic gives the original SiL type and array
length of each field in its `description`. The export does not add units,
frames, sample-time fields or entities.

### Time and order

- The MCAP log time and publish time of each Message are its source
  publication time in integer nanoseconds of Virtual time. Time zero is
  Virtual time zero; a viewer can show it as a date in 1970. The export
  applies no offset and never uses wall time.
- Messages keep their stored (Publish) order and count, also when stored
  times decrease.
- `sil.record_index` is the ordinal of the Message in the whole source, before
  Channel selection. `(source SHA-256, record_index)` is a stable locator of
  the source Message. It is different from the window-relative
  `message_index` of [Recording queries](recording-queries.md).
- Viewers sort by time. Their display order of Messages with the same time
  can differ from Publish order. Use `record_index` to tell them apart.

## The receipt

`<output>.receipt.json` has `"sil_visualization_export": 1` and these
members:

| Member | Content |
| --- | --- |
| `converter`, `profile` | Converter name, version, revision and MCAP library; profile name and version |
| `source` | File name, SHA-256, size and Manifest hash (or `null`) |
| `output` | File name, SHA-256, size and compression |
| `selection` | The selected Channels, or `null` for all |
| `channels` | Per Channel: topic, source Schema and its SHA-256, encodings, Message count and time bounds |
| `messages`, `bounds` | Total Message count; first and last publication time, or `null` without Messages |
| `numeric_policy` | The representation rules above |
| `completeness` | What is known about the Run that wrote the source |

Times in the receipt are decimal strings. The output metadata record
`sil_visualization` and each Channel's metadata repeat the source SHA-256
and profile, so the output identifies its source without the receipt.

To check the binding:

```sh
shasum -a 256 run.mcap run.visualization.mcap
python -c "import json; r = json.load(open('run.visualization.mcap.receipt.json')); print(r['source']['sha256'], r['output']['sha256'])"
```

### Completeness

`completeness.run_outcome` is `completed` or `failed` only when the Run's
provenance side-car `<source>.provenance.json` is next to the source and
names the source's SHA-256. Then `run_exit_code` is the Run's exit code.
Otherwise the outcome is `unknown`.

## Navigate a Recording from Python

Playback controllers and tool adapters read a finalized Recording through
`prepare_navigation`. It reads the `sil_pod` source directly; no export is
needed.

```python
from sil.visualization import prepare_navigation

with prepare_navigation("run.mcap", cache_dir="nav-cache") as nav:
    described = nav.describe()
    page = nav.messages(["vehicle/speed"], 0, 5_000_000_000)
    while not page.complete:
        page = nav.messages(["vehicle/speed"], 0, 5_000_000_000,
                            page.continuation)
    values = nav.snapshot(None, 2_000_000_000)
```

| Call | Result |
| --- | --- |
| `describe()` | Source identity (SHA-256, size, Manifest hash), Channel catalog with Message counts and bounds, overall bounds (`None` for an empty source), completeness and index path, version and size |
| `messages(channels, start_ns, end_ns, continuation=None, limits=None)` | One page of the Messages in `[start_ns, end_ns)`, ordered by publication time, then `record_index` |
| `snapshot(channels, time_ns)` | Per Channel, the latest Message at or before `time_ns`, or `None` when the Channel has no Message up to then |

- `channels=None` selects every Channel. An empty, unknown or repeated
  Channel name is refused with `SelectionError`.
- Times are integers in `0..2**64-1`; `end_ns` can be `2**64` to include the
  last possible time.
- Each Message is a `SourceRecord(record_index, channel,
  publication_time_ns, data)` with the original `sil_pod` payload bytes.
  `record_index` is the whole-source locator, also after Channel selection.
- Equal times are ordered by `record_index`. A snapshot takes the highest
  `record_index` among equal times. It never interpolates, never uses a
  later Message and never makes a missing value zero.

### Pages and continuations

A page holds at most `PageLimits.max_rows` Messages (default 1000) and
`PageLimits.max_bytes` payload bytes (default 8 MiB). `page.complete` is
true when no Message of the request follows. Otherwise
`page.continuation` resumes after the last Message. A continuation is valid
only for the same source, index version, Channel selection, interval and
order; any other use, or a malformed continuation, is refused with
`ContinuationError`. A Message larger than the page byte limit is refused
with `LimitExceeded`, never skipped. For a refused Message, the exception
names it in `record_index`, `channel`, `publication_time_ns` and
`size_bytes`.

`continuation_after(channels, start_ns, end_ns, time_ns, record_index)`
makes a continuation that resumes strictly after that position. A reader
uses it to continue from its own position, for example after a refused
Message. With `record_index` `2**63 - 1` it resumes after all Messages at
`time_ns`.

### The index

`prepare_navigation` hashes the source and builds a SQLite index of Message
locations in `cache_dir`, named by source SHA-256 and index version. The
payloads stay in the MCAP. The index is built in a temporary file and
published by rename when it is complete. A later preparation of the same
source hashes it again and reuses the index; an unreadable or mismatched
index is deleted and built again. The index is disposable: delete it at any
time.

- `progress` receives `Progress(scanned_bytes, total_bytes, messages)` after
  each chunk. Setting the `cancel` event stops preparation with
  `PreparationCancelled`.
- The source file identity (device, inode, size, modification time) is
  checked before and after preparation and around every read. A change is
  refused with `SourceError`.
- The index grows with the Message count, about 75 bytes per Message.
  `describe().index.size_bytes` reports it. A full disk, or a full
  `Limits.max_index_bytes` budget, raises `IndexStorageError`.
- After cancellation or any failure, no index and no temporary file stays.

`Limits` bounds the source reads. Sizes are checked before allocation or
decompression; oversize data is refused with `LimitExceeded`.

| Limit | Default | Applies to |
| --- | --- | --- |
| `max_decoded_bytes` | 64 MiB | One chunk, compressed and decompressed |
| `chunk_cache_bytes` | 128 MiB | Decoded chunks kept for later reads |
| `max_message_bytes` | 8 MiB | One Message payload |
| `max_index_bytes` | none | The index file |

SQLite caches and decoder metadata come in addition, so memory use is
bounded but not constant. `tools/navigation_bench.py` measures preparation
time, index size, peak RSS and seek latency on the spec fixture: 10 million
64-byte Messages on 16 Channels in 1 MiB chunks.
[navigation-results.md](bench/navigation-results.md) holds one measurement
(raw values in [navigation.json](bench/navigation.json)). It describes one
machine with a warm operating-system file cache; it is not a promise for
other machines or a cold disk.

## Play a Recording from Python

`open_playback` prepares navigation of a finalized Recording and returns a
`Playback` controller. The controller is headless and tool-independent: it
keeps the playback state and gives an ordered stream of notifications. A
tool adapter forwards the notifications to its viewer. No viewer, browser or
Foxglove SDK is necessary.

```python
from sil.visualization import open_playback
from sil.visualization.playback import Backfill, Commit, Message

with open_playback("run.mcap") as playback:
    playback.set_subscription(["vehicle/speed", "sparse"])
    state = playback.configure("seek-7", position_ns=2_000_000_000,
                               playing=False, rate=1.0)
    while playback.status().pending:
        playback.pump()
    for notification in playback.drain():
        if isinstance(notification, (Backfill, Message)):
            record = notification.record
            print(record.channel, record.publication_time_ns)
        elif isinstance(notification, Commit):
            print("cursor", notification.state.cursor_ns)
```

Without `cache_dir`, the index goes to `$XDG_CACHE_HOME/sil/navigation`, or
to `~/.cache/sil/navigation` when `XDG_CACHE_HOME` is not set. The index is
disposable; delete it at any time.

### Commands

| Call | Effect |
| --- | --- |
| `configure(request_id, position_ns=None, playing=None, rate=None)` | Seek, play or pause, and set the rate in one atomic operation. `None` keeps the current value. |
| `set_subscription(channels)` | Deliver only these Channels. New Channels are reconstructed at the cursor. |
| `status()` | The current `PlaybackState`. |
| `disconnect()` | The client went away: pause, drop the subscription, the queue and the request IDs. |
| `close()` | End the session and close the index. |

Playback starts paused at the first Message time, with no subscription. The
rate is a finite number from 0.1 to 10. A seek accepts any time from the
first to the last Message time, inclusive. A source without Messages has no
playback range: a seek is refused, and play ends at once.

`playback.description` is the `describe()` result of the source: identity,
Channels with their Schemas, bounds and completeness.

An invalid command raises `InvalidCommand`. Its `state` is the actual
state, which the command did not change.

`PlaybackState` holds the session and source identity (`session_id`,
`source_sha256`), the `generation`, `status`, the desired `playing` state,
`pending`, the committed `cursor_ns`, the pending `seek_ns`, the bounds
`start_ns` and `end_ns`, the `rate`, the last `request_id`, the
`subscription` and a `diagnostic`.

| `status` | Meaning |
| --- | --- |
| `paused` | Playback does not advance. |
| `playing` | Playback advances by elapsed time times the rate. |
| `buffering` | A reconstruction is pending, or the adapter drains slower than the rate. |
| `ended` | All Messages up to the last time are delivered. Play without a seek restarts at the first time. |
| `failed` | The source changed or the index is unusable. `diagnostic` tells why. Only `disconnect` and `close` are accepted. |
| `closed` | The session ended. |

### Request IDs

The controller keeps the last 256 accepted request IDs. A repeated request
ID with the same content returns its first result and does nothing again.
The same ID with other content raises `RequestConflict`. A refused request
is not kept. `request_id=None` is never kept.

After 256 newer requests, or after `disconnect`, an ID is new again. A
client that is not sure whether its request was applied, for example after
a reconnect, reads `status()` first. Then it sends a new request ID for the
change that it still needs.

### Notifications

`drain()` returns the notifications in order. One type per event:

| Notification | Meaning |
| --- | --- |
| `Reset(generation, position_ns)` | Discard all delivered history. A seek follows. |
| `Backfill(generation, record)` | The latest Message of a Channel at or before the cursor, with its original time and `record_index`. It is not a new publication. |
| `Message(generation, record)` | A Message that continuous playback crossed. |
| `Refused(...)` | A Message that a limit refused, in its place. `backfill` tells if it stands for a latest value. |
| `Commit(state)` | A reconstruction is complete; `state.cursor_ns` is the new cursor. |
| `StateChanged(state)` | The status changed without a command: buffering, ended, failed or closed. |

A seek to `t` gives `Reset`, the latest Message of each subscribed Channel
at or before `t`, then `Commit`. A Channel without a Message up to `t` gets
no `Backfill`: missing data is never a zero. Equal times resolve to the
highest `record_index`. After the commit, play starts strictly after `t`.

During play, each subscribed Message is delivered once, in
(publication time, `record_index`) order, also for equal times and for
stored times that decrease. A new subscription gets `Backfill` and `Commit`
for the new Channels only, without `Reset`.

A new seek, subscription change or disconnect starts a new generation. A
seek or disconnect also removes all queued notifications, and a
subscription change removes those of the removed Channels. A reconstruction that a
newer command replaced is discarded when it completes, so a late result
never overrides the newer command. At most one reconstruction runs, and the
latest command is the one pending replacement.

### Bounds and oversize Messages

`QueueLimits(max_messages=1024, max_bytes=16 MiB)` bounds the queued
Messages and their payload bytes. When the queue is full, playback stops
advancing and reports `buffering`. It continues when the adapter has
drained the queue, from the position where it stopped, so no Message is
skipped. A reconstruction is added as one unit, so the queue can hold one
more Message per subscribed Channel.

A Message larger than `Limits.max_message_bytes` or
`QueueLimits.max_bytes` is not delivered. A `Refused` notification takes
its place, with its Channel, time, `record_index`, size and the reason.
Playback then continues after it. To deliver such a Message, increase the
limits.

### Time and threads

The controller reads time from a monotonic clock in integer nanoseconds.
`open_playback(clock=...)` and `Playback(navigator, clock=...)` accept a
different clock, for example a test clock. A rate change continues from the
current playhead, so the cursor does not jump. Time spent pending or
buffering does not advance the playhead.

One thread does the work: it calls `pump()`, or `run()`, which calls
`pump()` until `close()`. Only that thread reads the index. All other calls
can come from any thread. The controller does not hold its lock while it
reads, so a new command can replace a reconstruction that is in progress.

## Serve a Recording to Foxglove

`sil playback serve` plays a Recording to Foxglove without an export. The
Foxglove playback bar (play, pause, seek, speed) controls SiL's headless
playback. SiL supplies no panel and no viewer.

The command needs the `foxglove` extra. Export and the other commands do not:

```sh
pip install 'sil[foxglove]'
sil playback serve run.mcap --adapter foxglove --listen 127.0.0.1:8765
```

The command validates and indexes the source (see [The index](#the-index)),
starts paused at the first instant, prints the address and serves until
Ctrl-C or SIGTERM. Exit codes: 0 stopped by a signal, 2 rejected input or
configuration (also a missing extra, a source without Messages, or a source
that ends after 2^32 s), 1 an I/O failure such as a port in use.

To connect:

1. Start the command. It prints `serving run.mcap to foxglove at
   ws://127.0.0.1:8765`.
2. In Foxglove, select **Open connection**, then **Foxglove WebSocket**, and
   enter the printed address.
3. Add panels on the topics. A topic is a Channel name; Messages are the
   same `sil_visualization_json` payloads as in the export.

The bridge serves one client at a time on the printed address. A second
client gets HTTP 409. Independent users or independent cursors need
separate `sil playback serve` processes. The default address is loopback.
Another `--listen` host makes the stream reachable from the network without
authentication.

The Foxglove SDK server behind the bridge also listens on a loopback port
of its own. A local process that connects to that port directly gets
copies of the data, without the no-loss guarantee, but no control: the
bridge ignores its playback requests and its subscriptions, and the SDK
answers each of its playback requests with an error status. Only the
client of the printed address controls playback.

### What the playback bar does

| Foxglove | SiL |
| --- | --- |
| Play, Pause, speed | One `configure`: the desired playing state and the rate (0.1 to 10). A request that only changes the speed keeps the playing state. |
| Seek | `configure` with a position in the source's inclusive range. The answer reports `Buffering` until the reconstruction committed. |
| Subscribe, unsubscribe a topic | The subscription is the set of the client's topics. A new topic gets the latest value at the cursor. |
| Disconnect | Playback pauses. The next client gets a new session ID, a reset and the latest values at the cursor. |

Each answer is the actual state. A refused request (for example a speed of
50) answers the unchanged state, with the request ID, and the Problems panel
shows a warning with the reason. The server time and every Message log time
are Virtual time in integer nanoseconds; time zero shows as the epoch.

A seek sends, in this order: all data sent before it, a playback state with
`did_seek` (Foxglove clears its panels), the latest Message per subscribed
topic at or before the seek time with its original time and locator, then
the committed state. A topic without a Message before the seek time stays
empty; SiL sends no zero in its place. Play continues strictly after the
seek time. `Ended` follows the final Message.

A Message that a limit refuses is not sent: the Problems panel names its
topic, time and `record_index`. The limits are the navigation limits, the
controller queue and an encoded-payload limit of 8 MiB.

### No silent loss

foxglove-sdk 0.30.3 keeps a bounded queue per client and drops the oldest
Message when it is full, without notice to the program. It also sends that
queue and its control queue (playback states, time) in no fixed order. The
adapter therefore puts a relay in front of the SDK server
(`adapters/foxglove_relay.py`). The relay reports which barrier statuses and
seek states the SDK actually wrote. The adapter keeps at most half the SDK
queue unacknowledged, so the SDK never drops a Message for the relay's
client. The relay also marks its client's playback requests and
subscriptions with one-time tokens, so a direct connection to the SDK port
cannot bypass this flow control. A slow client slows
the relay, the adapter stops taking Messages, and the controller reports
`Buffering`. A client that accepts no data for 30 s is disconnected; its
next connection starts with a reset and a fresh reconstruction.

### Limits of streaming

- History: Foxglove keeps only what the stream delivered since the last
  reset. A plot does not show the full range ahead of the playhead. For
  full-range plots, loops and offline use, open the
  [export](#export) as a file instead.
- Latest-value reconstruction suits field inspection. It does not rebuild
  state that accumulates over events, such as a 3D scene.
- Wide integers are decimal strings, for exact inspection, not plot series.

### Compatibility matrix

| Component | Version | Verified here | How |
| --- | --- | --- | --- |
| foxglove-sdk (Python) | 0.30.3, pinned in the `foxglove` extra | Yes | `tests/test_visualization_foxglove.py` against the SDK server |
| WebSocket subprotocol | `foxglove.sdk.v1` | Yes | Scripted client in the tests |
| WebSocket subprotocol | `foxglove.websocket.v1` | Refused by the SDK (HTTP 400) | Test `test_the_sdk_refuses_the_legacy_subprotocol` |
| WebSocket subprotocol | `foxglove.sdk.v1.1` | No | The relay reads no schema table; not exercised |
| Foxglove desktop or web application | none | No | Manual procedure below |

Verified with the scripted client against SDK 0.30.3: capabilities `time`
and `playbackControl`, the data range, the export's schemas, exact 64-bit
values as strings, log times equal to publication times, equal-time order
by `record_index`, the latest-value backfill (also before a sparse topic's
first Message), `did_seek` before the backfill, no value from before a seek
after its reset (also with data queued in the SDK), repeated seeks, pause,
speed, refused requests, subscription changes, a stalled client without
loss, a stall timeout, reconnect with a new session, a refused second
client, a direct connection to the SDK port without control, oversize
payloads and an unchanged source.

Not verified, because no Foxglove application is installed here:

- that the application offers `foxglove.sdk.v1` or `foxglove.sdk.v1.1`
  and connects;
- that its playback bar sends the requests above and shows the range, the
  time and the states;
- that `did_seek` clears its panels, also a Raw Messages panel on a topic
  without a value before the seek time;
- how it shows times near 2^32 s, equal-time Messages and `null` floats.

Procedure for these items:

1. `python tests/visualization_fixture.py qualification.mcap`
2. `sil playback serve qualification.mcap --adapter foxglove`
3. Connect as above. Add a Plot of `fields.speed_mps` on `vehicle/speed`
   and Raw Messages panels on `counter`, `sparse` and `burst`.
4. Play at 1x and 2x; pause; seek to 6 s, then to 2 s. Make sure that
   `sparse` is empty after the seek to 2 s (its only Message is at 5 s), and
   that `counter` shows `fields.count` `"9007199254741192"` at 2 s.
5. Close the application, connect again and make sure that the panels start
   empty and paused at the old cursor.
6. Record the application version and the results in this matrix.

## Open the output in Foxglove

The following procedure opens an export in the Foxglove desktop application
or web application. It needs no SiL server.

1. Write and export the qualification Recording from a source checkout:

   ```sh
   python tests/visualization_fixture.py qualification.mcap
   sil recording export qualification.mcap --profile visualization-json \
       -o qualification.visualization.mcap
   ```

   It holds 10 s of Virtual time: a 10 ms `vehicle/speed` signal, counters
   above 2^53, a `sensor` with non-finite readings, a three-Message
   equal-time Burst on `burst` every second, one `sparse` Message at 5 s,
   and arrays.

2. In Foxglove, select **Open local file** and select
   `qualification.visualization.mcap`.
3. Add a **Plot** panel with the field `fields.speed_mps` of the topic
   `vehicle/speed`. Use play, pause, the seek bar, the speed selection and
   loop.
4. Add a **Raw Messages** panel on `/counter`. Make sure that
   `fields.count` shows `"9007199254741492"` at 5 s, exactly.
5. Add a **Raw Messages** panel on `/sensor` and `/burst`. Record how the
   viewer shows `null` readings and the three Messages at one time.
6. Record the application version and the results with the evidence.

### Qualification status

No Foxglove application version is qualified yet. The output is verified
with the `mcap` Python reader: indexed time-range reads, schemas, metadata,
payloads and hashes. Steps 2 to 6 have not been done. Until they are done,
compatibility with Foxglove is the documented capability of its MCAP file
player with `json` and `jsonschema`, not a tested result. One open question
is how Foxglove treats `"type": ["number", "null"]` in a float field's
schema; record it during qualification.
