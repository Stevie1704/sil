"""Prepare the ADAS reference maneuvers: input Recordings, expected
Recordings and comparison contracts.

Each maneuver is two hand-authored CSV files in `maneuvers/`: the processed
radar, camera and ego-speed observations (`<maneuver>.csv`), and the expected
controller output, enumerated row by row from the profile's arithmetic
(`<maneuver>.expected.csv`). Nothing here runs the C application.

`mapping.json`, `expected-mapping.json` and `contract.json` name the Channels
of one maneuver without a prefix. One Run carries every maneuver side by side,
so this script prefixes each Channel with `<maneuver>.` and converts with
`sil-csv` (sil.csv_recording):

    python prepare.py OUTDIR

writes, per maneuver, `<maneuver>.inputs.mcap`, `<maneuver>.expected.mcap`,
their receipts and `<maneuver>.contract.json` into OUTDIR.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sil.csv_recording import convert

EXAMPLE_DIR = Path(__file__).resolve().parent
MANEUVER_DIR = EXAMPLE_DIR / "maneuvers"
MANEUVERS = ("clear", "hazard", "release", "unavailable", "boundaries")


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


def prepare(out_dir: Path, maneuvers: tuple[str, ...] = MANEUVERS) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for maneuver in maneuvers:
        _convert(EXAMPLE_DIR / "mapping.json", maneuver,
                 MANEUVER_DIR / f"{maneuver}.csv",
                 out_dir / f"{maneuver}.inputs.mcap",
                 out_dir / f"{maneuver}.inputs.receipt.json")
        _convert(EXAMPLE_DIR / "expected-mapping.json", maneuver,
                 MANEUVER_DIR / f"{maneuver}.expected.csv",
                 out_dir / f"{maneuver}.expected.mcap",
                 out_dir / f"{maneuver}.expected.receipt.json")
        (out_dir / f"{maneuver}.contract.json").write_text(
            json.dumps(_prefixed_contract(maneuver), indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path,
                        help="directory to write Recordings and contracts to")
    prepare(parser.parse_args().out_dir)
