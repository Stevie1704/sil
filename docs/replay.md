# Replay windows and long Recordings

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## Replay a selected window after a warm-up

A Run starts at Virtual time zero. To evaluate a part of a long recording,
do not seek a stateful vECU into it: its state would be wrong. Select the
window with `sil-window`, which writes a new Recording that starts at the
window. The vECU runs through a warm-up first, and only the rest is
evaluated. There is no state snapshot and no seek into a running vECU.

The worked example is in [examples/library/](../examples/library/):
`history.csv` is 3 s of recorded speed, `window.json` selects 0.5 s to 2.5 s
with a 1 s warm-up, and `window-no-warm-up.json` is the negative control:
the same evaluation interval with no warm-up. `window_contract.py` writes the
`sil-compare` contract from the `sil-window` receipt. With the staged
installation on `PATH`, from the checkout root:

```sh
workdir=$(mktemp -d "$HOME/sil-window.XXXXXX")
cc -shared -fPIC -O2 -o "$workdir/speed_filter.so" examples/library/speed_filter.c
sil-csv examples/library/mapping.json examples/library/history.csv \
    -o "$workdir/history.mcap" --receipt "$workdir/history.receipt.json"
sil-window examples/library/window.json "$workdir/history.mcap" \
    -o "$workdir/window.mcap" --receipt "$workdir/window.receipt.json"
python examples/library/manifest.py "$workdir/full.json" \
    --recording "$workdir/history.mcap" --library "$workdir/speed_filter.so" \
    --duration-ns 2500000000
python examples/library/manifest.py "$workdir/windowed.json" \
    --recording "$workdir/window.mcap" --library "$workdir/speed_filter.so" \
    --duration-ns 2000000000
sil-run "$workdir/full.json" -o "$workdir/full.mcap" --participant-timeout-ms 10000
sil-run "$workdir/windowed.json" -o "$workdir/windowed-1.mcap" --participant-timeout-ms 10000
sil-run "$workdir/windowed.json" -o "$workdir/windowed-2.mcap" --participant-timeout-ms 10000
cmp "$workdir/windowed-1.mcap" "$workdir/windowed-2.mcap"
python examples/library/window_contract.py "$workdir/window.receipt.json" \
    -o "$workdir/contract.json"
sil-compare "$workdir/contract.json" "$workdir/windowed-1.mcap" "$workdir/full.mcap"
```

`make example-window` runs the same sequence from the source tree. The full
history Run is the reference. Its Duration is the window's `end_ns`, because
the CSV origin is 0. The windowed Run's Duration is the receipt's
`duration_ns`. The comparison passes: after 100 warm-up Steps, the filter
state no longer depends on where the Run started. Repeat the sequence with
`window-no-warm-up.json` and `--duration-ns 1000000000`: the comparison
fails at the first evaluated Step, because the filter starts from its
initial state.

The window document is JSON. All times are integer nanoseconds on the source
Recording's time axis:

```json
{
  "sil_replay_window": 1,
  "source_origin_ns": 500000000,
  "replay_start_ns": 500000000,
  "evaluation_start_ns": 1500000000,
  "end_ns": 2500000000,
  "channels": ["ego.speed"],
  "max_gap_ns": 10000000
}
```

| Key | What it states |
| --- | --- |
| `source_origin_ns` | the source time of Virtual time zero: Virtual time = source time − origin |
| `replay_start_ns` | the first selected instant, and the start of the warm-up |
| `evaluation_start_ns` | the end of the warm-up and the start of the evaluation interval |
| `end_ns` | the end of the window, exclusive. It becomes the Duration: `end_ns` − origin |
| `channels` | the source Channels to carry. Other Channels are not in the output |
| `hold_initial` | optional: Channels that get a held initial value (see below) |
| `source_time_fields` | optional: per Channel, the `u64` or `i64` fields that hold nanoseconds on the source time axis |
| `max_gap_ns` | optional: the longest interval a Channel may go without a Message |

The instants must obey `source_origin_ns` ≤ `replay_start_ns` ≤
`evaluation_start_ns` < `end_ns`. An origin before the replay start keeps
that offset: the first Message is then after Virtual time zero.

What the window does, and what it does not do:

- **Selection.** The Messages in [`replay_start_ns`, `end_ns`) are selected.
  A Message at the replay start is in the warm-up; a Message at the
  evaluation start is evaluated; a Message at `end_ns` is not selected.
- **Order and values.** Messages keep their stored order, so Messages at one
  timestamp keep their Publish order. Payload bytes are copied unchanged, and
  the schemas are copied from the source.
- **Source-time fields.** A payload field that holds a source time is not
  changed unless `source_time_fields` names it. A named field is rebased like
  the log time. A result outside the field type's range is rejected. A held
  Message keeps the source time in its payload, so its rebased field is
  earlier than its log time.
- **Coverage.** Each selected Channel must cover the window on its own. A
  Channel whose first Message is after the replay start is missing history.
  A Channel whose last Message is before the window's last instant
  (`end_ns` − 1) is insufficient coverage. With `max_gap_ns`, a last Message
  up to `max_gap_ns` before `end_ns` covers the end, so a periodic source
  can end one Period early. A selected Channel with no Message in the
  window is an empty selection, also when it is in `hold_initial`: a held
  value does not fill it. All three are rejected. A Channel with
  Messages only in the warm-up is accepted; the receipt reports its
  evaluation coverage as 0 Messages.
- **Gaps.** A gap stays a gap. The receipt states each Channel's longest
  interval without a Message, counted from the replay start to `end_ns`.
  With `max_gap_ns`, a longer interval is rejected.
- **Held initial value.** A Channel in `hold_initial` with no Message at the
  replay start gets its latest earlier Message, published at the replay
  start before the window's own Messages, also before other Channels'
  Messages at that instant. A Channel without an earlier Message is
  rejected as missing history. The receipt names the source time
  of each held Message. Nothing else is held and nothing is interpolated.
- **Warm-up.** The window does not initialize the vECU. The vECU runs
  through the warm-up like any other part of the Run. Choose the warm-up
  from the vECU's memory: the example's slower filter keeps 5/6 of its
  state difference per Step. Choose `source_origin_ns` so that the Steps
  land on the source Steps you compare with.

The receipt names the preparer, the SHA-256 of the source Recording, the
window document and the output; the warm-up and evaluation intervals in
source and Virtual time; per Channel its span in the source, the message
count and the first and last Virtual time in each interval, the held Message
and the longest gap; the `duration_ns` for the replaying Manifest; and the
`evaluation_window` in Virtual time, both ends included, for a comparison
contract. Exclude the warm-up from every metric: use that evaluation window
in the contract, as `window_contract.py` does. An in-run KPI of a Test
participant must also start at the evaluation window; the window document
does not reach the participants. The example's Test participant checks
every output against its own model, from Virtual time zero, which is
correct in the warm-up too; it is not a comparison with the full history. The output Recording carries
the source and window digests as MCAP metadata. The same inputs give a
byte-identical Recording, and a changed window gives a different Recording
and so a different Manifest hash.

`sil-window` exits 0 on success and 2 when the window or the source is
rejected; nothing is written then.

## Replay a long Recording

A Replay participant does not keep its Recording in memory. Before any
participant steps, it reads the Recording twice: once to compare its SHA-256
with the Manifest, and once to decode every MCAP record. While the Run
advances, it reads the Recording again, one MCAP record at a time. Each read is a full or
partial pass over the file, so a long Recording costs read time, not memory.

- **Payload memory** is one read buffer per Replay participant. It grows to
  the largest MCAP record in the Recording and no further. This is the
  largest chunk, or the largest single Message outside a chunk. Chunks must
  be uncompressed: the kernel has no lz4 or zstd decoder, and it rejects a
  compressed chunk as a record that does not decode. It does not grow
  with the length of the Recording or with the Channels it does not replay.
  `sil-run-instrumented` reports it as
  `deterministic.replay_read_buffer.high_water_bytes`.
- **Metadata memory** is separate from payload memory, and it has no fixed
  limit. It grows with the number of chunks. The MCAP summary holds one chunk
  index per chunk, with one offset for each Channel in that chunk. It also
  holds every Channel and schema record.
- **A damaged Recording is a Manifest error** (exit 2), also when the Manifest
  hash matches it. The kernel rejects a Recording without its footer (cut
  short) or with an MCAP record that does not decode. It does this before
  any participant steps.
- **The validated file is the file that is replayed.** The Replay participant
  opens the Recording once and does all its reads through that open file.
  Renaming, deleting or replacing the path during the Run has no effect on
  the replay. Writing into the file during the Run changes its size or
  modification time; the next read sees that and the Run fails (exit 1) with
  "changed after it was validated". A write that keeps both the size and the
  modification time is not detected.
- **Timestamps are replayed as stored, not sorted.** Some Recordings store a
  Message after a Message with a later time. The Replay participant publishes
  the earlier Message at its own time, after the Messages stored before it.
  Its Slot is then earlier than the Slot before it. A Recording whose times start at or after the Duration publishes nothing.
  `sil-csv` rejects descending timestamps, so its Recordings are in time order.
