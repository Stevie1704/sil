# Visualize a Recording in an external viewer

[Documentation index](README.md)

`sil recording export` converts a finalized Recording into an indexed MCAP
file that an external file player, such as Foxglove, opens directly. The
player supplies play, pause, seek, speed, loops and plots. SiL supplies no
viewer, extension or server on this path.

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
