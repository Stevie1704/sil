# Recorded input and FMU replay

[Documentation index](README.md) · [Repository overview](../README.md)

Commands below run from the repository root unless stated otherwise.

## Replay a timestamped CSV recording

`sil recording csv` converts one timestamped CSV file into a Recording the Replay
participant accepts, under an explicit mapping document. CSV is the one
starter format; decoding and unit conversion stay in this converter, and the
kernel only sees an ordinary Recording. The worked example is in
[examples/csv/](../examples/csv/): `signals.csv`, its `mapping.json`, an
`observer.py` consumer that republishes every Message it receives, and the
`manifest.py` that replays the Recording into it.

With the staged installation on `PATH` (previous section), from the checkout
root:

```sh
workdir=$(mktemp -d)
sil recording csv examples/csv/mapping.json examples/csv/signals.csv \
    -o "$workdir/signals.mcap" --receipt "$workdir/signals.receipt.json"
sil recording csv examples/csv/mapping.json examples/csv/signals.csv \
    -o "$workdir/signals-2.mcap" --receipt "$workdir/signals-2.receipt.json"
cmp "$workdir/signals.mcap" "$workdir/signals-2.mcap"
python examples/csv/manifest.py "$workdir/replay.json" \
    --recording "$workdir/signals.mcap"
sil-run "$workdir/replay.json" -o "$workdir/run-1.mcap"
sil-run "$workdir/replay.json" -o "$workdir/run-2.mcap"
cmp "$workdir/run-1.mcap" "$workdir/run-2.mcap"
```

`make example-csv` runs the same sequence from the source tree. The two
receipts differ only in the Recording file name they record. The same CSV,
mapping and converter version give a byte-identical Recording, and the
Manifest over it gives byte-identical Run Recordings.

The mapping document is JSON:

```json
{
  "sil_csv_mapping": 1,
  "timestamp": {"column": "time_s", "unit": "s", "origin": 1695640000},
  "schemas": {"csv.Range": {"fields": [{"name": "range_m", "type": "f64"}]}},
  "channels": [
    {"channel": "target.range", "schema": "csv.Range",
     "fields": {"range_m": {"column": "range_raw", "scale": 0.125, "offset": -10}}}
  ]
}
```

- `timestamp` names the time column, its `unit` (`s`, `ms`, `us` or `ns`) and
  an optional `origin` in that unit (default 0). Replay time is
  `(cell − origin) × unit`, computed exactly in integer nanoseconds.
- `schemas` uses the Manifest's schema form. Declare
  the same schemas in the replaying Manifest: the Replay participant rejects a
  Channel whose recorded schema differs from the declared one.
- Each `channels` entry maps every field of its schema, and only those, to a
  column, with an optional `scale` (default 1) and `offset` (default 0):
  `value = cell × scale + offset`. Integer fields need integer cells, scale
  and offset. Float fields compute in binary64, then round to f32 for an f32
  field; the scale and offset themselves are rounded to binary64 first. One
  column may feed several fields, the timestamp column included.
- An array field (a field with `count`) takes `columns` instead of `column`:
  exactly `count` columns, element 0 first. Each element converts as a
  scalar field of the element type, with the field's one `scale` and
  `offset`. An empty element cell counts like any other empty cell: there is
  no partial array and no implicit zero.

The conversion rejects, with the row, line and column, rather than guess:

- a missing, empty or repeated column; a mapping with duplicate keys, an
  unmapped or unknown schema field, a duplicate Channel, or an unknown key;
- a timestamp that is not a plain decimal, is not a whole number of
  nanoseconds after the origin, is negative after the origin, overflows u64,
  or descends below the previous row's;
- a value that is not finite, does not fit its field type, or underflows
  from nonzero to zero;
- a row with the wrong number of cells, and a Channel with some but not all
  of its cells empty in one row. All-empty cells mean that row carries no
  Message of that Channel. Nothing is interpolated or filled in.

Messages are written in row order and, within one row, in mapping order;
rows that share a timestamp keep that order when the Replay participant
publishes them. The receipt records the converter name, version, source
revision and MCAP library; the SHA-256 of the CSV, the mapping and the
Recording; each Channel's message count and first and last time; and the time
bounds of the Recording. A converted Recording carries the source and mapping digests as MCAP
metadata instead of a Manifest hash: no Manifest produced it. Choose the
replaying Manifest's Duration after the
receipt's `last_ns`: the Replay participant does not publish Messages at or
after the Duration, and it drops them without an error.

Supported limits: one comma-delimited UTF-8 file with a header row; scalar
and fixed-size array fields of the existing schema types, one column per
element; one linear scale and offset per field;
times from 0 to 2^64 − 1 ns after the origin. There is no decoder for MDF,
ROS bags, BLF or DBC, and no variable-length or payload fields.

## Replay recorded input into one FMU

`sil fmi replay` writes the Manifest of a Run that replays a converted
Recording into one FMU. An authoring document states every choice of the
Run. The command checks the document against the FMU before anything runs,
then writes an ordinary canonical Manifest: a Replay participant, and one
Process participant whose command is the
[FMI importer](fmi.md#fmi-30-co-simulation-importer)'s. The helper adds no
contract. Every choice is visible in the Manifest, and a hand-written
Manifest with the same bytes is the same Run.
[examples/fmu-replay/](../examples/fmu-replay/) is the worked example:

| File | Role |
| --- | --- |
| `ego_motion.c`, `modelDescription.xml`, `package.py` | `EgoMotion`, a scalar FMU with units: input `acceleration` (m/s2), parameters `initial_speed` (m/s) and `initial_position` (m), outputs `speed` (m/s) and `position` (m) |
| `recorded.csv`, `mapping.json` | the recorded acceleration in cm/s², and the `sil recording csv` mapping that converts it to m/s² |
| `authoring.json` | the authoring document |
| `reference.csv`, `reference-mapping.json`, `contract.json` | the closed-form trajectory, computed by hand, and the `sil compare` contract |

With the staged installation on `PATH`, from the checkout root:

```sh
workdir=$(mktemp -d "$HOME/sil fmi replay.XXXXXX")
cc -shared -fPIC -O2 -o "$workdir/EgoMotion.so" examples/fmu-replay/ego_motion.c
python examples/fmu-replay/package.py "$workdir/EgoMotion.so" \
    -o "$workdir/EgoMotion.fmu"
sil recording csv examples/fmu-replay/mapping.json examples/fmu-replay/recorded.csv \
    -o "$workdir/recorded.mcap" --receipt "$workdir/recorded.receipt.json"
sil recording csv examples/fmu-replay/reference-mapping.json \
    examples/fmu-replay/reference.csv -o "$workdir/reference.mcap"
sil fmi replay examples/fmu-replay/authoring.json "$workdir/EgoMotion.fmu" \
    --recording "$workdir/recorded.mcap" -o "$workdir/fmu-replay.json" \
    --receipt "$workdir/authoring.receipt.json"
sil-run "$workdir/fmu-replay.json" -o "$workdir/run-1.mcap"
sil-run "$workdir/fmu-replay.json" -o "$workdir/run-2.mcap"
cmp "$workdir/run-1.mcap" "$workdir/run-2.mcap"
sil compare examples/fmu-replay/contract.json "$workdir/run-1.mcap" \
    "$workdir/reference.mcap"
```

`make example-fmu-replay` runs the same sequence from the source tree, and
authors the Manifest twice to show that the two are byte-identical.

The authoring document is JSON. Every key is required, and nothing has a
default:

```json
{
  "sil_fmu_replay": 1,
  "step_period_ns": 10000000,
  "duration_ns": 1000000000,
  "schemas": {"ego.Acceleration": {"fields": [{"name": "accel_mps2", "type": "f64"}]},
              "ego.Motion": {"fields": [{"name": "speed_mps", "type": "f64"},
                                        {"name": "position_m", "type": "f64"}]}},
  "channels": {
    "ego.accel": {"schema": "ego.Acceleration", "direction": "in",
                  "latency_ns": 0, "route": {"capacity": 1, "overflow": "fail"}},
    "ego.motion": {"schema": "ego.Motion", "direction": "out",
                   "latency_ns": 10000000}
  },
  "bind": [{"channel": "ego.accel", "field": "accel_mps2",
            "variable": "acceleration", "unit": "m/s2"}, "..."],
  "start": [{"variable": "initial_speed", "value": "20", "unit": "m/s"}, "..."],
  "hold": []
}
```

- `step_period_ns` is the FMU's communication step, and `duration_ns` is the
  Run's Duration.
- Each Channel states its `latency_ns`. Each `in` Channel also states its
  bounded subscriber `route`. The Recording replays every `in` Channel, and
  the FMU publishes every `out` Channel.
- Each `bind` entry becomes one `--bind <channel>:<field>=<variable>`
  argument. Each `start` entry becomes one `--start <variable>=<value>`
  argument, with the value text in the importer's spelling: a decimal
  number, `true` or `false`, or hexadecimal for a Binary. `unit` is the unit
  the value is in, or `null` for no unit.
- `hold` names the FMU inputs that keep the start value the FMU declares
  for the whole Run.

The command writes the FMU path as an argument of its own, resolved to an
absolute path, so `sil-run` resolves it and records its SHA-256 in the Run's
provenance side-car. The Recording is named by its absolute path and its
SHA-256, as `Manifest.add_replay` names it.

Before it writes a Manifest, the command rejects (exit 2) with the reason:

- an FMU the importer cannot drive;
- a mapping the importer would reject: an unknown variable, a variable of
  the other direction, a field type that does not carry the variable's type,
  a variable type the importer does not map, a start value that does not
  parse, and a Binary field or Binary start value above the variable's
  `maxSize`. These are the checks
  of [`sil fmi inspect`](fmi.md#inspecting-an-fmu-before-a-run). The importer makes
  the same checks when it initializes. The command does not load the FMU's
  binary;
- a start value for a variable that is not an input or a parameter;
- a stated unit that is not the unit the FMU variable declares. The
  importer converts no unit. Convert the recorded unit at the edge, with
  `sil recording csv`'s `scale` and `offset`, and state the FMU's unit. The example
  records cm/s² and converts with `"scale": 0.01`;
- an FMU input that is not bound, not given a start value and not held;
- a Recording that does not carry an `in` Channel, or carries it with a
  different schema;
- a missing Latency or route, a route on an `out` Channel, an unknown or
  duplicate key, and every value the Manifest builder refuses.
- a Manifest or receipt path that is the document, the FMU or the
  Recording, and a receipt path that is the Manifest path. The command
  compares the resolved paths.

The receipt (on standard output, or at `--receipt`) records the command's
version, the SHA-256 of the document, the FMU, the Recording and the
Manifest, and each binding, start value and held input with the FMU's type,
causality and unit. The receipt carries `"sil_fmu_replay_receipt": 1`.

What the Run does at the edges of the recorded input:

- **Input at zero.** Until the first recorded Message reaches the FMU, an
  input has its start value: the FMU's declared start, or the document's
  `start`. With `latency_ns` 0, a Message recorded at 0 reaches the first
  Step, which covers `[0, P]`. With a Latency of one Period `P`, it reaches
  the second Step, and the first Step uses the start value. The example
  declares 0; with one Period, its first speed is 20 m/s instead of
  20.015 m/s, and the comparison fails.
- **Between Messages.** A Step writes the newest Message of each input
  Channel. The FMU keeps that value until a newer Message arrives. Nothing
  is interpolated. A Message recorded between two Steps reaches the FMU at
  the next Step after it is visible.
- **Outputs.** The importer publishes at the start of a Step the values at
  its end, so the contract states `"actual_offset_ns"` of one Period.

Supported limits:

- **Fixed period.** The kernel steps the FMU on one fixed Period. The
  importer does not choose communication points: an FMU that asks for an
  event between two Steps fails the Run. Choose a Period that divides every
  instant the FMU must stop at.
- **Event profile.** One FMU, driven by the importer's single-FMU profile.
  A Clock is carried only as a clocked Binary payload of a `triggered`
  Clock. The command does not author a group of connected FMUs.
- **Types.** The importer's mapped types: `Float64`, `Boolean`, `Binary`,
  `Float32`, `Int32`, `UInt32`, `UInt64`, `UInt8` and `Int64`, each in the field type of its
  own width ([FMI importer](fmi.md#numeric-scalars)). Other types can be
  held at their declared start. `make example-fmu-numeric` replays the four
  numeric types into `Feedthrough`.
- **Arrays.** A `Float32`, `Float64`, `Int32`, `UInt32`, `UInt64`, `UInt8` or `Int64` array with
  literal dimensions is one Channel field with `count` equal to its value
  count, in row-major order ([FMI importer](fmi.md#fixed-size-numeric-arrays)).
  Its start value lists every value, separated by single spaces.
  `examples/fmu-array/` replays `[8]` and `[2,3]` arrays.
