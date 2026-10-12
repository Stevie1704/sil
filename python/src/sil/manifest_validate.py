"""`sil manifest validate`: check a Manifest without running it (#275).

`sil-run` stops at the first Manifest error. This check applies the same
rules, collects every error, and gives each one a JSON Pointer (RFC 6901) to
the value at fault and a short fix hint. It starts no Participant.

The rules mirror three places in the kernel, and
`tests/test_manifest_validate.py` runs both sides over one corpus of
Manifests so that they cannot drift apart:

- the loader, `kernel/src/manifest.cpp`: keys, types, ranges, references;
- the one-publisher-per-Channel rule;
- the artifact preflight, `kernel/src/provenance.cpp`, and the Replay
  participant's Recording hash: the files the Manifest names exist.

Some Manifest errors only a Run can find, because they need the artifact
loaded: a Native library that does not load or does not export
`sil_participant_init`, a Recording whose Channels or Schemas do not match,
an Arena the system cannot map, and a missing Clock shim. A bare Process
participant command is looked up on the PATH of this check; `sil-run` uses
its own PATH.

Exit codes are those of `sil-run` for a Manifest error: 0 if the Manifest is
valid, 2 if it is not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from ._schema_types import INT_RANGES, SIZES

REPORT_VERSION = 1
EXIT_INVALID = 2
MANIFEST_VERSION = 1

_U64 = (0, 2**64 - 1)
_SIZE = _U64  # the kernel's size_t on the 64-bit hosts it supports
_I32 = (-(2**31), 2**31 - 1)
_FLOAT32_MAX = 3.4028234663852886e38
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"  # the kernel's, for an empty PATH

_TOP_KEYS = ("sil_manifest", "duration_ns", "epoch_ns", "schemas", "channels",
             "participants")
_CHANNEL_KEYS = ("schema", "latency_ns", "transport", "slots", "interceptors")
_INTERCEPTOR_KEYS = ("kind", "start_ns", "end_ns", "delay_ns", "n", "field",
                     "value")
_INTERCEPTOR_KINDS = ("drop", "drop_nth", "delay", "override")
_PARTICIPANT_KEYS = {
    "native": ("type", "library", "config", "subscribes", "publishes", "shim"),
    "process": ("type", "command", "step_period_ns", "subscribes", "publishes",
                "priority", "shim", "sleep", "threads"),
    "replay": ("type", "recording", "recording_hash", "channels", "priority",
               "shim"),
}
_SHIM_KEYS = ("shim", "sleep", "threads")


@dataclass(frozen=True)
class Problem:
    """One Manifest error: where it is, what is wrong, and how to fix it."""

    path: str
    message: str
    hint: str


def _pointer(tokens: tuple[str, ...]) -> str:
    return "".join("/" + str(token).replace("~", "~0").replace("/", "~1")
                   for token in tokens)


def _describe(value) -> str:
    kind = {dict: "object", list: "array", str: "string", bool: "boolean",
            int: "integer", float: "number", type(None): "null"}[type(value)]
    return f"{kind} {json.dumps(value)}"


def _is_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class _Checker:
    """Collects the Problems of one Manifest document."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.problems: list[Problem] = []

    def add(self, path: tuple, message: str, hint: str) -> None:
        self.problems.append(Problem(_pointer(path), message, hint))

    # Value checks: each returns the value if it is valid, otherwise None.

    def typed(self, value, path, test, expected: str, hint: str):
        if test(value):
            return value
        self.add(path, f"expected {expected}, got {_describe(value)}", hint)
        return None

    def object(self, value, path):
        return self.typed(value, path, lambda v: isinstance(v, dict),
                          "an object", "Write a JSON object: {...}.")

    def array(self, value, path):
        return self.typed(value, path, lambda v: isinstance(v, list),
                          "an array", "Write a JSON array: [...].")

    def string(self, value, path):
        return self.typed(value, path, lambda v: isinstance(v, str),
                          "a string", "Write a JSON string in double quotes.")

    def boolean(self, value, path):
        return self.typed(value, path, lambda v: isinstance(v, bool),
                          "a boolean", "Write true or false.")

    def integer(self, value, path, bounds, *, minimum=None):
        lo, hi = bounds
        expected = (f"a non-negative integer in [0, {hi}]" if lo == 0
                    else f"an integer in [{lo}, {hi}]")
        if minimum is not None:
            expected, lo = f"an integer >= {minimum}", minimum
        return self.typed(value, path,
                          lambda v: _is_integer(v) and lo <= v <= hi, expected,
                          "Write a whole number without a decimal point, "
                          "in the range given.")

    def required(self, obj: dict, key: str, path):
        if key not in obj:
            self.add(path + (key,), f"missing required key '{key}'",
                     f"Add the key '{key}'.")
            return None
        return obj[key]

    def known_keys(self, obj: dict, path, allowed) -> None:
        for key in obj:
            if key not in allowed:
                self.add(path + (key,), f"unknown key '{key}'",
                         "Remove the key, or correct its spelling. Allowed "
                         "keys: " + ", ".join(allowed) + ".")

    def choice(self, value, path, choices, what: str):
        value = self.string(value, path)
        if value is not None and value not in choices:
            self.add(path, f"unknown {what} '{value}'",
                     "Use one of: " + ", ".join(choices) + ".")
            return None
        return value


def _check_document(doc, base_dir: Path) -> list[Problem]:
    c = _Checker(base_dir)
    if c.object(doc, ()) is None:
        return c.problems
    c.known_keys(doc, (), _TOP_KEYS)

    version = c.required(doc, "sil_manifest", ())
    if version is not None and c.integer(version, ("sil_manifest",),
                                         _I32) is not None:
        if version != MANIFEST_VERSION:
            c.add(("sil_manifest",),
                  f"unsupported sil_manifest version {version}",
                  f"Set sil_manifest to {MANIFEST_VERSION}.")
    duration = c.required(doc, "duration_ns", ())
    if duration is not None:
        duration = c.integer(duration, ("duration_ns",), _U64, minimum=1)
    if "epoch_ns" in doc:
        c.integer(doc["epoch_ns"], ("epoch_ns",), _U64)

    schemas = {}
    value = c.required(doc, "schemas", ())
    if value is not None and c.object(value, ("schemas",)) is not None:
        for name in sorted(value):
            schemas[name] = _check_schema(c, value[name], ("schemas", name))

    channels = {}
    value = c.required(doc, "channels", ())
    if value is not None and c.object(value, ("channels",)) is not None:
        for name in sorted(value):
            channels[name] = _check_channel(c, value[name], ("channels", name),
                                            schemas, duration)

    value = c.required(doc, "participants", ())
    if value is not None and c.object(value, ("participants",)) is not None:
        publishers = {}
        for name in sorted(value):
            path = ("participants", name)
            published = _check_participant(c, value[name], path, channels)
            for channel in published:
                if channel in publishers:
                    first = publishers[channel]
                    c.add(path, f"channel '{channel}' has more than one "
                                f"publisher: participant '{first}' and "
                                f"participant '{name}'",
                          "A Channel has one publisher. Remove the Channel "
                          "from one of the two participants.")
                else:
                    publishers[channel] = name
    return c.problems


def _check_schema(c: _Checker, schema, path) -> list[dict] | None:
    """The Schema's valid fields, or None if the Schema is not valid."""
    if c.object(schema, path) is None:
        return None
    c.known_keys(schema, path, ("fields",))
    fields = c.required(schema, "fields", path)
    if fields is None or c.array(fields, path + ("fields",)) is None:
        return None
    if not fields:
        c.add(path + ("fields",), "fields must be a non-empty array",
              "Declare at least one field: {\"name\": ..., \"type\": ...}.")
        return None
    valid, names, byte_size = True, set(), 0
    for index, field in enumerate(fields):
        field_path = path + ("fields", index)
        if c.object(field, field_path) is None:
            valid = False
            continue
        c.known_keys(field, field_path, ("name", "type", "count"))
        name = c.required(field, "name", field_path)
        name = None if name is None else c.string(name, field_path + ("name",))
        ftype = c.required(field, "type", field_path)
        if ftype is not None:
            ftype = c.choice(ftype, field_path + ("type",), tuple(SIZES),
                             "type")
        count = 1
        if "count" in field:
            count = c.integer(field["count"], field_path + ("count",), _SIZE,
                              minimum=1)
        if name is not None and name in names:
            c.add(field_path + ("name",), f"duplicate field name '{name}'",
                  "Give each field of a Schema its own name.")
            valid = False
        if name is None or ftype is None or count is None:
            valid = False
            continue
        names.add(name)
        byte_size += SIZES[ftype] * count
        if byte_size > _SIZE[1]:
            c.add(field_path, "Schema byte size overflows size_t",
                  "Make the field counts smaller.")
            return None
    return fields if valid else None


def _check_channel(c: _Checker, channel, path, schemas, duration):
    """The Channel's Schema fields, or None if they are not known."""
    if c.object(channel, path) is None:
        return None
    c.known_keys(channel, path, _CHANNEL_KEYS)
    fields = None
    schema = c.required(channel, "schema", path)
    if schema is not None and c.string(schema, path + ("schema",)) is not None:
        if schema not in schemas:
            c.add(path + ("schema",), f"references unknown schema '{schema}'",
                  "Declare the Schema under 'schemas', or correct the name. "
                  "Declared: " + (", ".join(sorted(schemas)) or "none") + ".")
        else:
            fields = schemas[schema]
    if "latency_ns" in channel:
        c.integer(channel["latency_ns"], path + ("latency_ns",), _U64)
    transport = "inline"
    if "transport" in channel:
        transport = c.choice(channel["transport"], path + ("transport",),
                             ("inline", "shm"), "transport")
    if "slots" in channel:
        if transport == "inline":
            c.add(path + ("slots",), "slots is only valid with shm transport",
                  "Remove 'slots', or set 'transport' to 'shm'.")
        slots = c.integer(channel["slots"], path + ("slots",), _SIZE,
                          minimum=1)
        if slots is not None and fields is not None and (
                _byte_size(fields) > _SIZE[1] // slots):
            c.add(path + ("slots",), "Arena byte size overflows size_t",
                  "Use fewer slots.")
    if "interceptors" in channel:
        interceptors = c.array(channel["interceptors"], path + ("interceptors",))
        for index, interceptor in enumerate(interceptors or []):
            _check_interceptor(c, interceptor, path + ("interceptors", index),
                               fields)
    return fields


def _byte_size(fields: list[dict]) -> int:
    return sum(SIZES[f["type"]] * f.get("count", 1) for f in fields)


def _check_interceptor(c: _Checker, interceptor, path, fields) -> None:
    if c.object(interceptor, path) is None:
        return
    c.known_keys(interceptor, path, _INTERCEPTOR_KEYS)
    kind = c.required(interceptor, "kind", path)
    if kind is not None:
        kind = c.choice(kind, path + ("kind",), _INTERCEPTOR_KINDS, "kind")
    start = 0
    if "start_ns" in interceptor:
        start = c.integer(interceptor["start_ns"], path + ("start_ns",), _U64)
    if "end_ns" in interceptor:
        end = c.integer(interceptor["end_ns"], path + ("end_ns",), _U64)
        if end is not None and start is not None and end <= start:
            c.add(path + ("end_ns",),
                  "window end_ns must be greater than start_ns",
                  "Set end_ns after start_ns, or remove end_ns to run the "
                  "window to the end of the Run.")
    if kind == "delay":
        value = c.required(interceptor, "delay_ns", path)
        if value is not None:
            c.integer(value, path + ("delay_ns",), _U64)
    elif kind == "drop_nth":
        value = c.required(interceptor, "n", path)
        if value is not None:
            c.integer(value, path + ("n",), _U64, minimum=1)
    elif kind == "override":
        _check_override(c, interceptor, path, fields)


def _check_override(c: _Checker, interceptor, path, fields) -> None:
    name = c.required(interceptor, "field", path)
    if name is None or c.string(name, path + ("field",)) is None:
        return
    value = c.required(interceptor, "value", path)
    if fields is None:
        return
    field = next((f for f in fields if f["name"] == name), None)
    if field is None:
        c.add(path + ("field",),
              f"override field '{name}' is not in the channel's schema",
              "Use a field of the Channel's Schema: "
              + ", ".join(f["name"] for f in fields) + ".")
        return
    if "count" in field:
        c.add(path + ("field",), f"override field '{name}' is a fixed-size "
                                 "array; only scalar fields can be overridden",
              "Override a scalar field of the Schema.")
        return
    if value is None:
        return
    ftype = field["type"]
    if ftype in INT_RANGES:
        c.integer(value, path + ("value",), INT_RANGES[ftype])
    elif c.typed(value, path + ("value",),
                 lambda v: _is_integer(v) or isinstance(v, float), "a number",
                 "Write a number.") is not None:
        if ftype == "f32" and abs(float(value)) > _FLOAT32_MAX:
            c.add(path + ("value",), "expected a number representable as f32",
                  f"Use a value with magnitude at most {_FLOAT32_MAX}.")


def _check_participant(c: _Checker, participant, path, channels) -> list[str]:
    """The Channels the Participant publishes."""
    if c.object(participant, path) is None:
        return []
    ptype = c.required(participant, "type", path)
    if ptype is None or c.string(ptype, path + ("type",)) is None:
        return []
    if ptype != "process":
        for key in _SHIM_KEYS:
            if key in participant:
                c.add(path + (key,),
                      f"{key} is only valid on process participants",
                      f"Remove '{key}'.")
    if ptype not in _PARTICIPANT_KEYS:
        c.add(path + ("type",), f"unknown type '{ptype}'",
              "Use one of: " + ", ".join(_PARTICIPANT_KEYS) + ".")
        return []
    c.known_keys(participant, path, _PARTICIPANT_KEYS[ptype])
    return {"native": _check_native, "process": _check_process,
            "replay": _check_replay}[ptype](c, participant, path, channels)


def _check_native(c: _Checker, participant, path, channels) -> list[str]:
    library = c.required(participant, "library", path)
    if library is not None and c.string(library, path + ("library",)) is not None:
        _check_file(c, library, path + ("library",), "Native library")
    for key in ("subscribes", "publishes"):
        if key not in participant:
            c.add(path + (key,), f"missing required key '{key}'",
                  f"Add '{key}', an array of Channel names; it may be empty.")
    if "config" in participant:
        c.object(participant["config"], path + ("config",))
    if "subscribes" in participant:
        _check_routes(c, participant["subscribes"], path + ("subscribes",),
                      channels)
    if "publishes" not in participant:
        return []
    return _check_channel_list(c, participant["publishes"],
                               path + ("publishes",), channels)


def _check_process(c: _Checker, participant, path, channels) -> list[str]:
    command = c.required(participant, "command", path)
    if command is not None and c.array(command, path + ("command",)) is not None:
        if not command:
            c.add(path + ("command",), "command must be a non-empty array",
                  "Name the executable first: [\"python3\", \"participant.py\"].")
        items = [c.string(item, path + ("command", index))
                 for index, item in enumerate(command)]
        if items and items[0] is not None:
            _check_executable(c, items[0], path + ("command", 0))
    period = c.required(participant, "step_period_ns", path)
    if period is not None:
        c.integer(period, path + ("step_period_ns",), _U64, minimum=1)
    if "subscribes" in participant:
        _check_routes(c, participant["subscribes"], path + ("subscribes",),
                      channels)
    if "priority" in participant:
        c.integer(participant["priority"], path + ("priority",), _I32)
    shim = False
    if "shim" in participant:
        shim = c.boolean(participant["shim"], path + ("shim",))
    for key, policies in (("sleep", ("immediate", "reject")),
                          ("threads", ("allow", "report", "reject"))):
        if key not in participant:
            continue
        if shim is False:
            c.add(path + (key,), f"{key} requires shim (the policy only "
                                 "applies to the virtual clock shim)",
                  f"Set 'shim' to true, or remove '{key}'.")
        c.choice(participant[key], path + (key,), policies, f"{key} policy")
    if "publishes" not in participant:
        return []
    return _check_channel_list(c, participant["publishes"],
                               path + ("publishes",), channels)


def _check_replay(c: _Checker, participant, path, channels) -> list[str]:
    recording = c.required(participant, "recording", path)
    if recording is not None:
        recording = c.string(recording, path + ("recording",))
    digest = c.required(participant, "recording_hash", path)
    if digest is not None:
        digest = c.string(digest, path + ("recording_hash",))
    if "priority" in participant:
        c.integer(participant["priority"], path + ("priority",), _I32)
    if recording is not None and digest is not None:
        _check_recording(c, recording, digest, path)
    replayed = c.required(participant, "channels", path)
    if replayed is None or c.array(replayed, path + ("channels",)) is None:
        return []
    if not replayed:
        c.add(path + ("channels",), "channels must be a non-empty array",
              "Name at least one recorded Channel to replay.")
    return _check_channel_list(c, replayed, path + ("channels",), channels)


def _check_channel_list(c: _Checker, value, path, channels) -> list[str]:
    names = []
    for index, item in enumerate(c.array(value, path) or []):
        name = c.string(item, path + (index,))
        if name is not None and _check_reference(c, name, path + (index,),
                                                 channels, names):
            names.append(name)
    return names


def _check_routes(c: _Checker, value, path, channels) -> None:
    seen: list[str] = []
    for index, route in enumerate(c.array(value, path) or []):
        route_path = path + (index,)
        if isinstance(route, str):
            name = route
        elif c.object(route, route_path) is None:
            continue
        else:
            c.known_keys(route, route_path, ("channel", "capacity", "overflow"))
            name = c.required(route, "channel", route_path)
            if name is not None:
                name = c.string(name, route_path + ("channel",))
            _check_route_policy(c, route, route_path)
        if name is not None and _check_reference(c, name, route_path,
                                                 channels, seen):
            seen.append(name)


def _check_route_policy(c: _Checker, route: dict, path) -> None:
    if ("capacity" in route) != ("overflow" in route):
        c.add(path, "'capacity' and 'overflow' must either both be present "
                    "or both be absent for an unbounded route",
              "Give both 'capacity' and 'overflow', or neither.")
        return
    if "capacity" not in route:
        return
    c.integer(route["capacity"], path + ("capacity",), _SIZE, minimum=1)
    overflow = c.string(route["overflow"], path + ("overflow",))
    if overflow == "blocking":
        c.add(path + ("overflow",), "blocking overflow is unsupported because "
                                    "the sequential scheduler cannot activate "
                                    "the consumer while the publisher is "
                                    "blocked",
              "Use 'fail' or 'drop_newest'.")
    elif overflow is not None and overflow not in ("fail", "drop_newest"):
        c.add(path + ("overflow",), f"unknown overflow policy '{overflow}'",
              "Use 'fail' or 'drop_newest'.")


def _check_reference(c: _Checker, name: str, path, channels, seen) -> bool:
    if name not in channels:
        c.add(path, f"references unknown channel '{name}'",
              "Declare the Channel under 'channels', or correct the name. "
              "Declared: " + (", ".join(sorted(channels)) or "none") + ".")
        return False
    if name in seen:
        c.add(path, f"lists channel '{name}' twice", "Remove the repeat.")
        return False
    return True


# The artifact preflight. Paths resolve against the Manifest directory, as in
# the kernel, so the check gives the same result from any working directory.

def _check_file(c: _Checker, name: str, path, what: str) -> Path | None:
    candidate = c.base_dir / name
    if name and candidate.is_file():
        return candidate
    c.add(path, f"{what} '{name}' is not a file",
          "Relative paths resolve against the Manifest's directory. Build the "
          "file, or correct the path.")
    return None


def _check_executable(c: _Checker, command: str, path) -> None:
    if not command or "/" in command:
        _check_file(c, command, path, "executable")
        return
    for entry in (os.environ.get("PATH") or _DEFAULT_PATH).split(":"):
        candidate = c.base_dir / entry / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return
    c.add(path, f"executable '{command}' was not found on PATH",
          "sil-run looks up a bare command on its own PATH. Activate the "
          "environment that has it, or give a path.")


def _check_recording(c: _Checker, recording: str, digest: str, path) -> None:
    file = _check_file(c, recording, path + ("recording",), "Recording")
    if file is None:
        return
    with open(file, "rb") as f:
        actual = hashlib.file_digest(f, "sha256").hexdigest()
    if actual != digest:
        c.add(path + ("recording_hash",),
              f"recording hash {actual} does not match manifest {digest}",
              "Build the Manifest again from the Recording you mean to "
              "replay.")


def _parse_int(text: str):
    # The kernel's JSON parser keeps an integer outside the 64-bit ranges as a
    # floating-point number, which no integer key accepts.
    value = int(text)
    return value if -(2**63) <= value <= _U64[1] else float(text)


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"number overflow parsing '{text}'")
    return value


def _reject_constant(text: str):
    raise ValueError(f"invalid literal '{text}'")


def validate(path: Path) -> list[Problem]:
    """Every Problem of the Manifest at `path`; an empty list if it is valid."""
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as error:
        return [Problem("", f"cannot open manifest file: {error}",
                        "Check the path.")]
    except UnicodeDecodeError as error:
        return [Problem("", f"invalid JSON: {error}", "Save the file as UTF-8.")]
    try:
        doc = json.loads(text, parse_int=_parse_int, parse_float=_parse_float,
                         parse_constant=_reject_constant)
    except ValueError as error:
        return [Problem("", f"invalid JSON: {error}",
                        "Write the Manifest with the sil.manifest builder.")]
    return _check_document(doc, path.absolute().parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sil manifest validate",
        description="Check a Manifest without running it, and list every "
                    "error with its JSON path and a fix hint.",
        allow_abbrev=False,
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--json", action="store_true",
                        help="write the sil_manifest_validation report to "
                             "stdout")
    args = parser.parse_args(argv)
    problems = validate(args.manifest)
    if args.json:
        report = {"sil_manifest_validation": REPORT_VERSION,
                  "manifest": str(args.manifest),
                  "valid": not problems,
                  "errors": [asdict(problem) for problem in problems]}
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    elif problems:
        for problem in problems:
            where = f"{problem.path}: " if problem.path else ""
            sys.stderr.write(f"{args.manifest}: {where}{problem.message}\n"
                             f"  hint: {problem.hint}\n")
        sys.stderr.write(f"{len(problems)} error(s)\n")
    else:
        sys.stdout.write(f"valid: {args.manifest}\n")
    return EXIT_INVALID if problems else 0


if __name__ == "__main__":
    sys.exit(main())
