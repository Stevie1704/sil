"""Audit where `alks_r157_expected.json` came from, against esmini itself.

The vendor expectation this proof checks its trajectory against is transcribed
by hand from esmini's own smoke test, and the smoke test asserts four ALKS
safety models at once in one merged recording. Two claims therefore sit
between the upstream project and the reference file, and both are checked
here rather than asserted in prose:

- **Transcription.** Every sample in the reference file appears verbatim in
  the smoke test's expected rows, under the declared object ids.
- **Identification.** The id block the samples were taken from is the one
  produced by the safety model the scenario file actually declares. The
  merged recording's block order is the merge order of four files, not the
  order of the model list in the test, so this is settled by running all four
  models and seeing which reproduces the transcribed row.

Nothing here judges a Run. It judges the reference file, so a mistranscribed
value cannot pass as a vendor expectation.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
from pathlib import Path

# esmini prints its banner and log to stdout from C, and its own options are
# documented as unset on the next scenario run — which this script performs
# four times. The adapter answers that the same way and says why; here it
# keeps the report separable from the library's chatter.
_report = os.fdopen(os.dup(1), "w")
os.dup2(2, 1)
sys.stdout = _report

sys.path.insert(0, "/opt/consumer/participants")

from esmini_participant import SE_ScenarioObjectState, _bind

# The smoke test's csv rows: time, id, name, x, y, z, h, p, r, speed, ...
CSV_ROW = re.compile(
    r"\^(?P<t>\d+\.\d+), (?P<id>\d+), (?P<name>\w+), "
    r"(?P<x>-?\d+\.\d+), (?P<y>-?\d+\.\d+), -?\d+\.\d+, "
    r"(?P<h>-?\d+\.\d+), -?\d+\.\d+, -?\d+\.\d+, (?P<speed>-?\d+\.\d+)"
)
MODELS = ("ReferenceDriver", "Regulation", "FSM", "RSS")
# The smoke test drives ReferenceDriver without the cruise property; every
# other model with it.
CRUISE = {model: model != "ReferenceDriver" for model in MODELS}


class TranscriptionError(AssertionError):
    """The reference file does not say what the upstream project says."""


def vendor_rows(smoke_test: Path) -> dict[tuple[str, int, str], dict[str, str]]:
    """(t, object id, object name) -> the expected values, as written."""
    rows = {}
    for match in CSV_ROW.finditer(smoke_test.read_text()):
        key = (match.group("t"), int(match.group("id")), match.group("name"))
        rows[key] = match.groupdict()
    return rows


def check_transcription(
    reference: dict, rows: dict, object_ids: dict[str, int]
) -> int:
    """Every transcribed sample is a row the smoke test actually asserts."""
    for sample in reference["samples"]:
        name = sample["object"]
        key = (f"{sample['t_s']:.3f}", object_ids[name], name)
        row = rows.get(key)
        if row is None:
            raise TranscriptionError(
                f"the smoke test asserts no row for {name} at "
                f"{sample['t_s']} s under id {object_ids[name]}"
            )
        for field, column in (
            ("x_m", "x"),
            ("y_m", "y"),
            ("heading_rad", "h"),
            ("speed_mps", "speed"),
        ):
            if float(row[column]) != sample[field]:
                raise TranscriptionError(
                    f"{name} {field} at {sample['t_s']} s is transcribed as "
                    f"{sample[field]}, but the smoke test asserts "
                    f"{row[column]}"
                )
    return len(reference["samples"])


def declared_model(scenario: Path) -> str:
    match = re.search(r'<Property name="model" value="(\w+)"/>', scenario.read_text())
    if match is None:
        raise TranscriptionError(f"{scenario} declares no ALKS model")
    return match.group(1)


def ego_state_at(library, scenario_xml: str, steps: int) -> dict[str, float]:
    """Run one scenario variant and read the ego out at the given Step."""
    library.SE_SetSeed(0)
    if library.SE_InitWithString(scenario_xml.encode(), 0, 0, 0, 0) != 0:
        raise TranscriptionError("SE_InitWithString rejected the scenario")
    try:
        ego = library.SE_GetIdByName(b"Ego")
        for _ in range(steps):
            if library.SE_StepDT(0.01) != 0:
                raise TranscriptionError("SE_StepDT failed")
        state = SE_ScenarioObjectState()
        library.SE_GetObjectState(ego, ctypes.byref(state))
        return {"x_m": state.x, "speed_mps": state.speed}
    finally:
        library.SE_Close()


def check_identification(
    library_path: str, scenario: Path, resources: Path, reference: dict
) -> str:
    """The transcribed block belongs to the model the scenario declares."""
    expected = next(
        sample
        for sample in reference["samples"]
        if sample["object"] == "Ego" and sample["t_s"] == max(
            other["t_s"] for other in reference["samples"]
        )
    )
    library = ctypes.CDLL(library_path)
    _bind(library)
    library.SE_AddPath.argtypes = [ctypes.c_char_p]
    library.SE_AddPath.restype = ctypes.c_int
    library.SE_InitWithString.argtypes = [ctypes.c_char_p] + [ctypes.c_int] * 4
    library.SE_InitWithString.restype = ctypes.c_int
    library.SE_LogToConsole(False)
    library.SE_SetLogFilePath(b"")
    for path in ("xodr", "xosc/Catalogs/Vehicles", "xosc/Catalogs/Controllers"):
        library.SE_AddPath(str(resources / path).encode())

    source = scenario.read_text()
    steps = round(expected["t_s"] * 100)
    matching = []
    for model in MODELS:
        variant = re.sub(
            r'<Property name="model" value=".*?"/>',
            f'<Property name="model" value="{model}"/>',
            source,
        )
        variant = re.sub(
            r'<Property name="cruise" value=".*?"/>',
            f'<Property name="cruise" value="{str(CRUISE[model]).lower()}"/>',
            variant,
        )
        state = ego_state_at(library, variant, steps)
        print(
            f"  {model:16s} ego x={state['x_m']:.3f} m "
            f"speed={state['speed_mps']:.3f} m/s"
        )
        if (
            abs(state["x_m"] - expected["x_m"]) <= 0.01
            and abs(state["speed_mps"] - expected["speed_mps"]) <= 0.01
        ):
            matching.append(model)

    declared = declared_model(scenario)
    if matching != [declared]:
        raise TranscriptionError(
            f"the transcribed rows at {expected['t_s']} s are reproduced by "
            f"{matching or 'no model'}, but the scenario declares {declared!r}"
        )
    return declared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--smoke-test", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument(
        "--object-id",
        action="append",
        required=True,
        metavar="NAME=ID",
        help="the merged-recording id the samples were transcribed from",
    )
    args = parser.parse_args(argv)

    reference = json.loads(args.reference.read_text())
    object_ids = {}
    for entry in args.object_id:
        name, _, value = entry.partition("=")
        object_ids[name] = int(value)

    try:
        transcribed = check_transcription(
            reference, vendor_rows(args.smoke_test), object_ids
        )
        print(f"transcription: {transcribed} samples match the smoke test")
        print("identification: reproducing the transcribed row per model")
        model = check_identification(
            args.library, args.scenario, args.resources, reference
        )
        print(f"identification: only {model!r} reproduces it, as declared")
    except TranscriptionError as error:
        sys.stderr.write(f"check_transcription: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
