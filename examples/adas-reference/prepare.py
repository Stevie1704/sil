"""Prepare the ADAS reference maneuvers: input Recordings, expected
Recordings and comparison contracts.

Each maneuver is hand-authored CSV files in `maneuvers/`: the processed
radar and camera object lists and the ego speed (`<maneuver>.csv`), and the
expected controller output, enumerated row by row from the profile's
arithmetic (`<maneuver>.expected.csv`). A maneuver can also have expected
outputs for named experiments over the same inputs, such as an Interceptor
or a changed Latency (`<maneuver>.<experiment>.expected.csv`). Nothing here
runs the C application.

An authored row is one activation: `time_ms,radar,camera,ego_speed_mps`.
A list cell is empty for no Message at that time, `invalid` for a list with validity 0,
`empty` for a valid list without objects, or up to eight objects separated
by `;`. A radar object is `id x_m y_m relative_vx_mps confidence`, a camera
object `id x_m y_m confidence`. The ego cell is empty, `invalid`, or a speed.

`expand` writes the flat Schema form of each row: the header (Sample time
`time_ms`, the sensor ID, frame 1, the row index as sequence, the count and
the validity), the active objects first, and zero in every inactive element.
A list longer than the capacity is rejected, never truncated.

`mapping.json`, `expected-mapping.json` and `contract.json` name the Channels
of one maneuver without a prefix. One Run carries every maneuver side by side,
so this script prefixes each Channel with `<maneuver>.` and converts with
`sil-csv` (sil.csv_recording):

    python prepare.py OUTDIR

writes, per maneuver, `<maneuver>.inputs.csv` (the expanded form),
`<maneuver>.inputs.mcap`, `<maneuver>[.<experiment>].expected.mcap`, their
receipts and `<maneuver>.contract.json` into OUTDIR.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from sil.csv_recording import convert

EXAMPLE_DIR = Path(__file__).resolve().parent
MANEUVER_DIR = EXAMPLE_DIR / "maneuvers"
MANEUVERS = ("clear", "hazard", "release", "unavailable", "boundaries",
             "occupancy", "turnover", "ordering", "cadence", "freshness")

CAPACITY = 8
EGO_FRAME_ID = 1
OBJECT_FIELDS = ("object_id", "x_m", "y_m", "relative_vx_mps", "confidence")
# Per sensor: its ID and the authored fields of one object. The camera
# reports no speed; its relative_vx_mps stays 0.
SENSORS = {
    "radar": (1, OBJECT_FIELDS),
    "camera": (2, ("object_id", "x_m", "y_m", "confidence")),
}


class PreparationError(ValueError):
    """An authored maneuver that does not fit the profile's shapes."""


def _columns() -> list[str]:
    """The expanded CSV's header: the columns mapping.json reads, in order."""
    mapping = json.loads((EXAMPLE_DIR / "mapping.json").read_text())
    columns = [mapping["timestamp"]["column"]]
    for channel in mapping["channels"]:
        for spec in channel["fields"].values():
            columns += spec.get("columns", [spec.get("column")])
    return list(dict.fromkeys(columns))


def _list(sensor: str, cell: str, t_ns: int, sequence: int,
          where: str) -> dict[str, str]:
    if cell == "":
        return {}
    sensor_id, authored = SENSORS[sensor]
    validity, objects = "1", []
    if cell == "invalid":
        validity = "0"
    elif cell != "empty":
        objects = [o.split() for o in cell.split(";")]
    if len(objects) > CAPACITY:
        raise PreparationError(
            f"{where}: {sensor} lists {len(objects)} objects, more than the "
            f"capacity {CAPACITY}; a list is never truncated")
    row = {f"{sensor}_t_ns": str(t_ns), f"{sensor}_sensor_id": str(sensor_id),
           f"{sensor}_frame_id": str(EGO_FRAME_ID),
           f"{sensor}_sequence": str(sequence),
           f"{sensor}_count": str(len(objects)),
           f"{sensor}_validity": validity}
    for i in range(CAPACITY):
        values = dict.fromkeys(OBJECT_FIELDS, "0")
        if i < len(objects):
            if len(objects[i]) != len(authored):
                raise PreparationError(
                    f"{where}: {sensor} object {i} has {len(objects[i])} "
                    f"values, not {len(authored)} ({' '.join(authored)})")
            values.update(zip(authored, objects[i]))
        for field, value in values.items():
            row[f"{sensor}_{field}_{i}"] = value
    return row


def _ego(cell: str, t_ns: int, sequence: int) -> dict[str, str]:
    if cell == "":
        return {}
    valid = cell != "invalid"
    return {"ego_t_ns": str(t_ns), "ego_sequence": str(sequence),
            "ego_validity": "1" if valid else "0",
            "ego_speed_mps": cell if valid else "0"}


def expand(source: Path, out: Path) -> None:
    """Write the flat Schema form of the authored maneuver `source`."""
    columns = _columns()
    with source.open(newline="") as f:
        rows = list(csv.DictReader(f))
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, columns, restval="", lineterminator="\n")
        writer.writeheader()
        for sequence, authored in enumerate(rows):
            where = f"{source.name} row {sequence + 1}"
            t_ns = int(authored["time_ms"]) * 1_000_000
            writer.writerow({
                "time_ms": authored["time_ms"],
                **_list("radar", authored["radar"], t_ns, sequence, where),
                **_list("camera", authored["camera"], t_ns, sequence, where),
                **_ego(authored["ego_speed_mps"], t_ns, sequence),
            })


def _prefixed_mapping(template: Path, maneuver: str) -> dict:
    mapping = json.loads(template.read_text())
    for entry in mapping["channels"]:
        entry["channel"] = f"{maneuver}.{entry['channel']}"
    return mapping


def _prefixed_contract(maneuver: str) -> dict:
    contract = json.loads((EXAMPLE_DIR / "contract.json").read_text())
    contract["channels"] = {
        f"{maneuver}.{channel}": {
            **rule, "reference_channel": f"{maneuver}.{rule['reference_channel']}",
        }
        for channel, rule in contract["channels"].items()
    }
    return contract


def _convert(template: Path, maneuver: str, source: Path, out: Path,
             receipt: Path) -> None:
    mapping = out.with_suffix(".mapping.json")
    mapping.write_text(json.dumps(_prefixed_mapping(template, maneuver),
                                  indent=2) + "\n")
    receipt.write_text(json.dumps(convert(mapping, source, out), indent=2)
                       + "\n")


def expectations(maneuver: str) -> list[str]:
    """The names of a maneuver's expected outputs: `<maneuver>` and each
    `<maneuver>.<experiment>`."""
    return sorted(path.name.removesuffix(".expected.csv")
                  for path in MANEUVER_DIR.glob(f"{maneuver}.*expected.csv"))


def prepare(out_dir: Path, maneuvers: tuple[str, ...] = MANEUVERS) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for maneuver in maneuvers:
        expanded = out_dir / f"{maneuver}.inputs.csv"
        expand(MANEUVER_DIR / f"{maneuver}.csv", expanded)
        _convert(EXAMPLE_DIR / "mapping.json", maneuver, expanded,
                 out_dir / f"{maneuver}.inputs.mcap",
                 out_dir / f"{maneuver}.inputs.receipt.json")
        for name in expectations(maneuver):
            _convert(EXAMPLE_DIR / "expected-mapping.json", maneuver,
                     MANEUVER_DIR / f"{name}.expected.csv",
                     out_dir / f"{name}.expected.mcap",
                     out_dir / f"{name}.expected.receipt.json")
        (out_dir / f"{maneuver}.contract.json").write_text(
            json.dumps(_prefixed_contract(maneuver), indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path,
                        help="directory to write Recordings and contracts to")
    prepare(parser.parse_args().out_dir)
