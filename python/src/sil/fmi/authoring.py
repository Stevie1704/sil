"""`sil fmi replay`: author a Run that replays recorded input into one FMU.

An authoring document states every choice of the Run, and this command turns
it, an FMU and a converted Recording into an ordinary canonical Manifest. It
adds no contract of its own: the Manifest holds a Replay participant and one
Process participant whose command is the Importer's, `python -m sil.fmi`,
with the FMU path as an argument of its own and one `--bind` and `--start`
per choice. The runner resolves that path and digests it into the Run's
provenance, as it does for any Manifest that names an FMU.

    {"sil_fmu_replay": 1,
     "step_period_ns": 10000000, "duration_ns": 1000000000,
     "schemas": {...},
     "channels": {
       "<in>": {"schema": "...", "direction": "in", "latency_ns": 0,
                "route": {"capacity": 1, "overflow": "fail"}},
       "<out>": {"schema": "...", "direction": "out", "latency_ns": 0}},
     "bind": [{"channel": "<in>", "field": "...", "variable": "...",
               "unit": "m/s2"}],
     "start": [{"variable": "...", "value": "20", "unit": "m/s"}],
     "hold": ["<input variable>"]}

Nothing is inferred and nothing has a default. Every Channel states its
Latency, and every input Channel states its bounded subscriber route. The
Recording replays every input Channel.

Everything is checked before a Manifest is written, and nothing is loaded:

* the FMU and the proposed mapping get the inspection's verdict, which is the
  verdict the Importer reaches when it initializes;
* a start value is set only on an input or a parameter. An array's start
  lists every value, separated by single spaces, in row-major order;
* each binding and each start value states the unit of its FMU variable.
  The Importer converts no unit. When the recorded unit differs, convert it
  at the edge (sil recording csv's scale and offset) and state the FMU's unit;
* each FMU input is bound, started, or held at its declared start value;
* the Recording carries each input Channel with the schema declared here.

A receipt records the digests of the document, the FMU, the Recording and
the Manifest, and each binding, start value and held input with its unit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mcap.exceptions import McapError

from sil import build_info
from sil.fmi.documents import (
    AuthoringError,
    array as _array,
    exact_object as _object,
    file_sha256 as _file_sha256,
    load,
    read as _read,
    require_distinct,
    sha256 as _sha256,
    string as _string,
    unit as _unit,
)
from sil.fmi.inspection import (
    COMPATIBLE,
    MAPPING_VERSION,
    array_record,
    inspect,
)
from sil.manifest import Manifest, ManifestError, SubscriberRoute
from sil.recording import UnknownRecordingFormat, read_schemas

DOCUMENT_VERSION = 1
RECEIPT_VERSION = 1
PROG = "sil fmi replay"
# The name the files it writes carry; it stays as the first release wrote it.
AUTHOR = "sil-fmu-replay"

REPLAY = "replay"
FMU = "fmu"

_DOCUMENT_KEYS = {"sil_fmu_replay", "step_period_ns", "duration_ns",
                  "schemas", "channels", "bind", "start", "hold"}
_CHANNEL_KEYS = {"in": {"schema", "direction", "latency_ns", "route"},
                 "out": {"schema", "direction", "latency_ns"}}
# The causalities FMI 3.0 lets an importer set before initialization.
_STARTABLE = ("input", "parameter", "structuralParameter")


def author(document: str | Path, fmu: str | Path, recording: str | Path,
           out: str | Path) -> dict:
    """Write the Manifest for `document` over `fmu` and `recording` to `out`;
    return the receipt. Nothing is written when the Run is rejected."""
    document, fmu = Path(document), Path(fmu)
    recording, out = Path(recording), Path(out)
    require_distinct(out, "Manifest", {"document": document, "fmu": fmu,
                                       "recording": recording})
    document_bytes = _read(document, "authoring document")
    doc = _parse(document_bytes, document)
    _check_schemas(doc)
    variables, report = _inspected(fmu, doc)
    _require_startable(doc, variables)
    _require_units(doc, variables)
    _require_inputs_declared(doc, variables)
    inputs = [name for name, channel in doc["channels"].items()
              if channel["direction"] == "in"]
    _require_recorded(recording, inputs, doc)
    text = _manifest(doc, fmu, recording, inputs).to_json()
    try:
        out.write_text(text)
    except OSError as e:
        raise AuthoringError(f"cannot write Manifest {str(out)!r}: {e}") from e
    return _receipt(doc, variables, report, {
        "document": {"file": document.name, "sha256": _sha256(document_bytes)},
        "fmu": {"file": fmu.name, "sha256": _file_sha256(fmu),
                "model_name": report["facts"]["model_name"],
                "instantiation_token":
                    report["facts"]["instantiation_token"]},
        "recording": {"file": recording.name,
                      "sha256": _file_sha256(recording)},
        "manifest": {"file": out.name, "sha256": _sha256(text.encode())},
    })


def _receipt(doc: dict, variables: dict[str, dict], report: dict,
             files: dict) -> dict:
    """The record of the authored Run: its files, and every choice with the
    type, causality and unit the FMU declares for it."""
    return {
        "sil_fmu_replay_receipt": RECEIPT_VERSION,
        "author": {"name": AUTHOR, "version": build_info.__version__,
                   "revision": build_info.SOURCE_REVISION},
        **files,
        "bindings": [
            {"channel": b["channel"], "field": b["field"],
             **_declared(variables[b["variable"]])}
            for b in doc["bind"]
        ],
        "starts": [
            {**_declared(variables[s["variable"]]), "value": s["value"]}
            for s in doc["start"]
        ],
        "held": [
            {"variable": name, "start": variables[name]["start"],
             "unit": variables[name]["unit"]}
            for name in doc["hold"]
        ],
    }


def _declared(variable: dict) -> dict:
    """What the FMU declares about one variable, as the receipt records it.

    A variable that declares dimensions also records them and its
    flattened value count.
    """
    return {"variable": variable["name"], "type": variable["type"],
            "causality": variable["causality"], "unit": variable["unit"],
            **array_record(variable)}


# Document ----------------------------------------------------------------------


def _parse(data: bytes, path: Path) -> dict:
    doc = load(data, path, "authoring document")
    try:
        return _document(doc)
    except AuthoringError as e:
        raise AuthoringError(f"authoring document {str(path)!r}: {e}") from None


def _document(doc) -> dict:
    doc = _object(doc, "the document", _DOCUMENT_KEYS)
    version = doc["sil_fmu_replay"]
    if isinstance(version, bool) or version != DOCUMENT_VERSION:
        raise AuthoringError(
            f"'sil_fmu_replay' must be {DOCUMENT_VERSION}, got {version!r}"
        )
    if not isinstance(doc["channels"], dict):
        raise AuthoringError("'channels' must be an object")
    for name, channel in doc["channels"].items():
        _channel(name, channel)
    bind = _array(doc["bind"], "'bind'")
    if not bind:
        raise AuthoringError(
            "'bind' must be a non-empty array: every binding is explicit"
        )
    for index, binding in enumerate(bind):
        context = f"bind[{index}]"
        _object(binding, context, {"channel", "field", "variable", "unit"})
        for key in ("channel", "field", "variable"):
            _string(binding[key], f"{context} {key!r}")
        _unit(binding["unit"], f"{context} 'unit'")
    for index, start in enumerate(_array(doc["start"], "'start'")):
        context = f"start[{index}]"
        _object(start, context, {"variable", "value", "unit"})
        _string(start["variable"], f"{context} 'variable'")
        _string(start["value"], f"{context} 'value'")
        _unit(start["unit"], f"{context} 'unit'")
    hold = _array(doc["hold"], "'hold'")
    for index, name in enumerate(hold):
        _string(name, f"hold[{index}]")
    if len(set(hold)) != len(hold):
        raise AuthoringError("'hold' names a variable twice")
    return doc


def _channel(name: str, channel) -> None:
    context = f"channel {name!r}"
    if not isinstance(channel, dict):
        raise AuthoringError(f"{context} must be an object")
    direction = channel.get("direction")
    if direction not in _CHANNEL_KEYS:
        raise AuthoringError(
            f"{context} declares direction {direction!r}; a direction is "
            f"'in' or 'out'"
        )
    _object(channel, context, _CHANNEL_KEYS[direction])
    if direction == "in":
        _object(channel["route"], f"{context} 'route'",
                {"capacity", "overflow"})


# The FMU -----------------------------------------------------------------------


def _inspected(fmu: Path, doc: dict) -> tuple[dict[str, dict], dict]:
    """Each FMU variable as the inspection reports it, and the report.

    The proposed mapping is the one the Manifest's init line and command
    carry, so the inspection's verdict on it is the Importer's.
    """
    mapping = {
        "sil_fmi_mapping": MAPPING_VERSION,
        "schemas": doc["schemas"],
        "channels": {
            name: {"schema": channel["schema"],
                   "direction": channel["direction"]}
            for name, channel in doc["channels"].items()
        },
        "bind": _bind_arguments(doc),
        "start": _start_arguments(doc),
    }
    report = inspect(fmu, mapping)
    if report["unusable"]:
        raise AuthoringError(
            f"the Importer cannot drive FMU {str(fmu)!r}: "
            + "; ".join(report["unusable"])
        )
    if report["verdict"] != COMPATIBLE:
        raise AuthoringError(
            f"FMU {str(fmu)!r} rejects the mapping: "
            f"{report['mapping']['rejection']}"
        )
    return {v["name"]: v for v in report["variables"]}, report


def _check_schemas(doc: dict) -> None:
    """The Manifest's own schema and Channel rules, before the FMU's."""
    try:
        manifest = Manifest(duration_ns=1)
        manifest.add_schemas(doc["schemas"])
        for name, channel in doc["channels"].items():
            manifest.add_channel(name, schema=channel["schema"])
    except ManifestError as e:
        raise AuthoringError(str(e)) from None


def _bind_arguments(doc: dict) -> list[str]:
    return [f"{b['channel']}:{b['field']}={b['variable']}" for b in doc["bind"]]


def _start_arguments(doc: dict) -> list[str]:
    return [f"{s['variable']}={s['value']}" for s in doc["start"]]


def _require_startable(doc: dict, variables: dict[str, dict]) -> None:
    for start in doc["start"]:
        causality = variables[start["variable"]]["causality"]
        if causality not in _STARTABLE:
            raise AuthoringError(
                f"start value for FMU variable {start['variable']!r}: its "
                f"causality is {causality!r}; an importer sets only an input "
                f"or a parameter before initialization"
            )


def _require_units(doc: dict, variables: dict[str, dict]) -> None:
    """Each stated unit is the unit its FMU variable declares."""
    for b in doc["bind"]:
        _require_unit(
            f"Channel {b['channel']!r} field {b['field']!r}", b["unit"],
            variables[b["variable"]],
            "the Importer converts no unit, so convert it at the edge "
            "(sil recording csv's scale and offset) and state {unit}",
        )
    for s in doc["start"]:
        _require_unit(
            f"start value for FMU variable {s['variable']!r}", s["unit"],
            variables[s["variable"]], "state the value in {unit}",
        )


def _require_unit(subject: str, stated: str | None, variable: dict,
                  remedy: str) -> None:
    declared = variable["unit"]
    if stated == declared:
        return
    if declared is None:
        raise AuthoringError(
            f"{subject} is stated in {stated!r}, but FMU variable "
            f"{variable['name']!r} declares no unit; state its unit as null"
        )
    stated_text = "states no unit" if stated is None else (
        f"is stated in {stated!r}")
    raise AuthoringError(
        f"{subject} {stated_text}, but FMU variable {variable['name']!r} is "
        f"in {declared!r}; {remedy.format(unit=repr(declared))}"
    )


def _require_inputs_declared(doc: dict, variables: dict[str, dict]) -> None:
    """Every FMU input is bound, started or held, and each held one is an
    input nothing else declares.

    A bound clocked variable drives its Clock, so the Clock is declared too.
    """
    bound = {b["variable"] for b in doc["bind"]}
    bound |= {clock for name in bound for clock in variables[name]["clocks"]}
    started = {s["variable"] for s in doc["start"]}
    for name in doc["hold"]:
        variable = variables.get(name)
        if variable is None:
            raise AuthoringError(
                f"'hold' holds FMU variable {name!r}, which the FMU does not "
                f"declare"
            )
        if variable["causality"] != "input":
            raise AuthoringError(
                f"'hold' holds FMU variable {name!r}, whose causality is "
                f"{variable['causality']!r}; only an input is held at its "
                f"declared start"
            )
        if name in bound or name in started:
            also = "bound" if name in bound else "given a start value"
            raise AuthoringError(
                f"'hold' holds FMU variable {name!r}, which is also {also}"
            )
    declared = bound | started | set(doc["hold"])
    missing = [name for name, variable in variables.items()
               if variable["causality"] == "input" and name not in declared]
    if missing:
        names = ", ".join(map(repr, missing))
        subject = (f"FMU input variable {names} is" if len(missing) == 1
                   else f"FMU input variables {names} are")
        raise AuthoringError(
            f"{subject} neither bound, started nor held; bind it to a Channel "
            f"field, give it a start value, or hold it at its declared start"
        )


# The Recording and the Manifest ------------------------------------------------


def _require_recorded(recording: Path, inputs: list[str], doc: dict) -> None:
    if not inputs:
        raise AuthoringError(
            f"the document declares no input-direction Channel, so the Run "
            f"would replay nothing from recording {str(recording)!r}"
        )
    try:
        recorded = read_schemas(recording)
    except (OSError, ValueError, McapError, UnknownRecordingFormat) as e:
        raise AuthoringError(
            f"cannot read recording {str(recording)!r}: {e}"
        ) from e
    for channel in inputs:
        schema = doc["schemas"][doc["channels"][channel]["schema"]]
        if channel not in recorded:
            raise AuthoringError(
                f"recording {str(recording)!r} carries no Channel "
                f"{channel!r}, which the document declares as an input"
            )
        if recorded[channel] != schema:
            raise AuthoringError(
                f"Channel {channel!r} is recorded with schema "
                f"{json.dumps(recorded[channel], sort_keys=True)}, and the "
                f"document declares {json.dumps(schema, sort_keys=True)}"
            )


def _manifest(doc: dict, fmu: Path, recording: Path,
              inputs: list[str]) -> Manifest:
    """The canonical Manifest of the authored Run."""
    command = ["python3", "-m", "sil.fmi", str(fmu.resolve())]
    for argument in _bind_arguments(doc):
        command += ["--bind", argument]
    for argument in _start_arguments(doc):
        command += ["--start", argument]
    channels = doc["channels"]
    try:
        manifest = Manifest(duration_ns=doc["duration_ns"])
        manifest.add_schemas(doc["schemas"])
        for name, channel in channels.items():
            manifest.add_channel(name, schema=channel["schema"],
                                 latency_ns=channel["latency_ns"])
        manifest.add_replay(REPLAY, recording=recording.resolve(),
                            channels=inputs)
        manifest.add_process(
            FMU,
            command=command,
            step_period_ns=doc["step_period_ns"],
            subscribes=[
                SubscriberRoute(name, capacity=channels[name]["route"]["capacity"],
                                overflow=channels[name]["route"]["overflow"])
                for name in inputs
            ],
            publishes=[name for name, channel in channels.items()
                       if channel["direction"] == "out"],
        )
        # The builder checks participant routes and Channels only here, and
        # a rejection must come before anything is written.
        manifest.to_doc()
    except ManifestError as e:
        raise AuthoringError(str(e)) from None
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Write the Manifest of a Run that replays a Recording "
                    "into one FMU, from an authoring document.",
        allow_abbrev=False,
    )
    parser.add_argument("document", type=Path,
                        help="the authoring document (sil_fmu_replay 1)")
    parser.add_argument("fmu", type=Path, help="the FMU archive")
    parser.add_argument("--recording", type=Path, required=True,
                        help="the Recording that replays every input Channel")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="the Manifest to write")
    parser.add_argument("--receipt", type=Path, default=None,
                        help="write the receipt here instead of to standard "
                             "output")
    args = parser.parse_args(argv)
    try:
        if args.receipt is not None:
            require_distinct(args.receipt, "receipt", {
                "manifest": args.output, "document": args.document,
                "fmu": args.fmu, "recording": args.recording,
            })
        receipt = author(args.document, args.fmu, args.recording, args.output)
    except AuthoringError as e:
        sys.stderr.write(f"{PROG}: error: {e}\n")
        return 2
    text = json.dumps(receipt, indent=2) + "\n"
    if args.receipt is None:
        sys.stdout.write(text)
        return 0
    try:
        args.receipt.write_text(text)
    except OSError as e:
        sys.stderr.write(f"{PROG}: error: cannot write receipt "
                         f"{str(args.receipt)!r}: {e}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
