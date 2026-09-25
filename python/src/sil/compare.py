"""`sil-compare`: one Recording against a reference, under an explicit contract.

A comparison contract states everything the comparison decides; nothing is
inferred from the data:

* which Channels are compared, and for each one which reference Channel holds
  its expected values;
* how a Message's recorded time maps to the time it describes, per Channel and
  per side: observation time = recorded time + offset. An FMU output published
  at the start of a Step describes the end of that Step, and another Channel of
  the same Run may describe its own publication time, so no offset is shared;
* the exact observation times — a list or a start/stop/step grid, both ends
  included — and one evaluation window common to every Channel, which leaves a
  warm-up unobserved;
* a rule for every field of the actual schema: `"exact"` for an integer field,
  `{"atol": a, "rtol": r}` for a float field, or `"ignore"`.

A float field passes where `abs(actual - reference) <= atol + rtol *
abs(reference)`. A NaN or infinity on either side fails; there is no rule that
accepts one. At each observation time both sides must hold exactly one
Message: a missing one fails, and so do two, because choosing between them
would be a guess. A Message whose observation time is not in the contract is
not observed. Nothing is interpolated and no tolerance is fitted.

A field the contract does not name, a field it names that the schema does not
declare, and a rule that does not fit the field's type fail the comparison as
wrong coverage.

The comparison says whether two trajectories agree within a contract; it says
nothing about whether either Run reproduces. That is the determinism check's
question, answered by `sil-check` bit-comparing two Recordings of one Manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from mcap.exceptions import McapError

from sil._schema_types import INT_RANGES
from sil.recording import UnknownRecordingFormat, read_records, read_schemas
from sil.schema import MessageType

CONTRACT_VERSION = 1
REPORT_VERSION = 1
PROG = "sil-compare"

EXIT_FAIL = 1
EXIT_USAGE = 2

PASS = "pass"
FAIL = "fail"
EXACT = "exact"
IGNORE = "ignore"

DETERMINISM = (
    "not judged: a reference comparison says whether two trajectories agree "
    "within the contract, not whether either Run reproduces; a determinism "
    "check (sil-check) bit-compares two Recordings of one Manifest"
)

_CONTRACT_KEYS = {"sil_comparison", "evaluation", "channels"}
_CHANNEL_REQUIRED = {"actual_offset_ns", "reference_offset_ns", "observations",
                     "fields"}
_GRID_KEYS = {"start_ns", "stop_ns", "step_ns"}


class ContractError(ValueError):
    """A contract document that states no comparison."""


class RecordingError(ValueError):
    """A Recording that cannot be read."""


@dataclass(frozen=True)
class Tolerance:
    atol: float
    rtol: float

    def allowed(self, reference: float) -> float:
        return self.atol + self.rtol * abs(reference)


@dataclass(frozen=True)
class ChannelContract:
    name: str
    reference_channel: str
    actual_offset_ns: int
    reference_offset_ns: int
    # The observation times inside the evaluation window, ascending.
    times_ns: tuple[int, ...]
    rules: dict[str, Tolerance | str]

    def compared(self) -> list[tuple[str, Tolerance | str]]:
        return [(f, rule) for f, rule in self.rules.items() if rule != IGNORE]


@dataclass(frozen=True)
class Contract:
    path: Path
    sha256: str
    from_ns: int
    to_ns: int
    channels: tuple[ChannelContract, ...]


# -- the contract ---------------------------------------------------------------

def read_contract(path: str | Path) -> Contract:
    """A contract document, checked for everything it has to state."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ContractError(f"cannot read contract {str(path)!r}: {error}")

    def refuse(problem: str) -> ContractError:
        return ContractError(f"contract {str(path)!r} {problem}")

    try:
        document = json.loads(data, object_pairs_hook=_unique_keys)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise refuse(f"is not JSON: {error}")
    except _RepeatedKey as error:
        raise refuse(f"repeats key {error.args[0]!r}")
    _object(document, "", _CONTRACT_KEYS, set(), refuse)
    if document["sil_comparison"] != CONTRACT_VERSION:
        raise refuse(
            f"declares sil_comparison {document['sil_comparison']!r}; "
            f"this command reads version {CONTRACT_VERSION}"
        )
    window = _object(document["evaluation"], "key 'evaluation' ",
                     {"from_ns", "to_ns"}, set(), refuse)
    from_ns = _integer(window["from_ns"], "evaluation from_ns", refuse)
    to_ns = _integer(window["to_ns"], "evaluation to_ns", refuse)
    if from_ns > to_ns:
        raise refuse("has an evaluation window that ends before it starts")
    channels = document["channels"]
    if not isinstance(channels, dict) or not channels:
        raise refuse("compares no Channel")
    return Contract(
        path=path, sha256=hashlib.sha256(data).hexdigest(),
        from_ns=from_ns, to_ns=to_ns,
        channels=tuple(_channel(name, spec, (from_ns, to_ns), refuse)
                       for name, spec in channels.items()),
    )


def _channel(name: str, spec, window: tuple[int, int], refuse) -> ChannelContract:
    context = f"channel {name!r} "
    spec = _object(spec, context, _CHANNEL_REQUIRED, {"reference_channel"}, refuse)
    reference = spec.get("reference_channel", name)
    if not isinstance(reference, str):
        raise refuse(f"{context}key 'reference_channel' is not a string")
    times = [t for t in _times(spec["observations"], context, refuse)
             if window[0] <= t <= window[1]]
    if not times:
        raise refuse(f"{context}observes nothing inside the evaluation window")
    fields = spec["fields"]
    if not isinstance(fields, dict):
        raise refuse(f"{context}key 'fields' is not an object")
    return ChannelContract(
        name=name, reference_channel=reference,
        actual_offset_ns=_integer(spec["actual_offset_ns"],
                                  f"{context}actual_offset_ns", refuse),
        reference_offset_ns=_integer(spec["reference_offset_ns"],
                                     f"{context}reference_offset_ns", refuse),
        times_ns=tuple(times),
        rules={field: _rule(rule, f"{context}field {field!r} ", refuse)
               for field, rule in fields.items()},
    )


def _times(spec, context: str, refuse) -> list[int]:
    if isinstance(spec, dict) and set(spec) == {"times_ns"}:
        times = spec["times_ns"]
        if not isinstance(times, list) or not times:
            raise refuse(f"{context}times_ns is not a non-empty list")
        times = [_integer(t, f"{context}times_ns", refuse) for t in times]
        if any(a >= b for a, b in zip(times, times[1:])):
            raise refuse(f"{context}times_ns is not strictly ascending")
        return times
    if isinstance(spec, dict) and set(spec) == _GRID_KEYS:
        start, stop, step = (_integer(spec[k], f"{context}{k}", refuse)
                             for k in ("start_ns", "stop_ns", "step_ns"))
        if step <= 0:
            raise refuse(f"{context}step_ns is not positive")
        if start > stop:
            raise refuse(f"{context}stop_ns is before start_ns")
        return list(range(start, stop + 1, step))
    raise refuse(
        f"{context}key 'observations' takes either times_ns or start_ns, "
        f"stop_ns and step_ns"
    )


def _rule(rule, context: str, refuse) -> Tolerance | str:
    if rule in (EXACT, IGNORE):
        return rule
    if isinstance(rule, dict):
        rule = _object(rule, context, {"atol", "rtol"}, set(), refuse)
        return Tolerance(*(_bound(rule[k], f"{context}{k}", refuse)
                           for k in ("atol", "rtol")))
    raise refuse(
        f"{context}has rule {rule!r}; a rule is 'exact', 'ignore' or "
        f'{{"atol": ..., "rtol": ...}}'
    )


class _RepeatedKey(ValueError):
    """A JSON object that states one key twice; json keeps only the last."""


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    for key in keys:
        if keys.count(key) > 1:
            raise _RepeatedKey(key)
    return dict(pairs)


def _object(value, context: str, required: set, optional: set, refuse) -> dict:
    if not isinstance(value, dict):
        raise refuse(f"{context}is not a JSON object")
    for key in value:
        if key not in required | optional:
            raise refuse(f"{context}has unknown key {key!r}")
    for key in sorted(required - set(value)):
        raise refuse(f"{context}is missing key {key!r}")
    return value


def _integer(value, context: str, refuse) -> int:
    if type(value) is not int:
        raise refuse(f"{context} is not an integer: {value!r}")
    return value


def _bound(value, context: str, refuse) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise refuse(f"{context} is not a finite number >= 0: {value!r}")
    return float(value)


# -- the comparison -------------------------------------------------------------

def compare(contract: Contract, actual: str | Path, reference: str | Path) -> dict:
    """The report on `actual` against `reference` under `contract`.

    Raises RecordingError for a Recording that cannot be read."""
    actual, reference = Path(actual), Path(reference)
    actual_schemas = _schemas(actual)
    reference_schemas = _schemas(reference)
    coverage = []
    usable = []
    for channel in contract.channels:
        problems = _coverage(channel, actual_schemas.get(channel.name),
                             reference_schemas.get(channel.reference_channel))
        coverage += problems
        if not problems:
            usable.append(channel)
    actual_messages = _messages(
        actual, actual_schemas,
        [(c, c.name, c.actual_offset_ns) for c in usable])
    reference_messages = _messages(
        reference, reference_schemas,
        [(c, c.reference_channel, c.reference_offset_ns) for c in usable])
    tally = _Tally()
    channels = {}
    for index, channel in enumerate(contract.channels):
        counts = _counts(channel)
        if channel in usable:
            _compare_channel(channel, index, actual_messages[channel.name],
                             reference_messages[channel.name], counts, tally)
        channels[channel.name] = counts
    return {
        "sil_comparison_report": REPORT_VERSION,
        "comparison": "reference",
        "determinism": DETERMINISM,
        "verdict": FAIL if coverage or tally.count else PASS,
        "contract": {"path": str(contract.path), "sha256": contract.sha256},
        "actual": _identity(actual),
        "reference": _identity(reference),
        "evaluation": {"from_ns": contract.from_ns, "to_ns": contract.to_ns},
        "coverage": coverage,
        "channels": channels,
        "divergences": tally.count,
        "first_divergence": tally.first,
    }


class _Tally:
    """How many divergences there are, and the earliest one."""

    def __init__(self):
        self.count = 0
        self.first = None
        self._key = None

    def add(self, key: tuple, divergence: dict) -> None:
        self.count += 1
        if self._key is None or key < self._key:
            self._key, self.first = key, divergence


def _counts(channel: ChannelContract) -> dict:
    return {
        "reference_channel": channel.reference_channel,
        "observations": len(channel.times_ns),
        "checked": 0, "failed": 0, "nonfinite": 0,
        "missing_actual": 0, "missing_reference": 0,
        "ambiguous_actual": 0, "ambiguous_reference": 0,
    }


def _compare_channel(channel: ChannelContract, index: int, actual: dict,
                     reference: dict, counts: dict, tally: _Tally) -> None:
    rules = channel.compared()
    for observation in channel.times_ns:
        at = _Observation(channel, index, observation, rules,
                          actual.get(observation, []),
                          reference.get(observation, []))
        if at.present(counts, tally):
            counts["checked"] += 1
            counts["failed"] += at.compare_fields(counts, tally)


@dataclass(frozen=True)
class _Observation:
    """One observation time of one Channel, and the Messages each side holds
    there as (recorded time, values)."""

    channel: ChannelContract
    index: int
    observation_ns: int
    rules: list
    actual: list
    reference: list

    def present(self, counts: dict, tally: _Tally) -> bool:
        """Whether each side holds exactly one Message; each gap is a
        divergence."""
        for side, messages in (("actual", self.actual),
                               ("reference", self.reference)):
            if not messages:
                counts[f"missing_{side}"] += 1
                self._diverge(tally, f"missing-{side}")
            elif len(messages) > 1:
                counts[f"ambiguous_{side}"] += 1
                self._diverge(tally, f"ambiguous-{side}")
        return len(self.actual) == 1 and len(self.reference) == 1

    def compare_fields(self, counts: dict, tally: _Tally) -> bool:
        """Judge every compared field; whether any of them diverged."""
        (published, actual), (recorded, expected) = self.actual[0], self.reference[0]
        failed = False
        for field_index, (field, rule) in enumerate(self.rules):
            kind, details = _judge(actual[field], expected[field], rule)
            if kind is not None:
                failed = True
                counts["nonfinite"] += kind == "nonfinite"
                self._diverge(tally, kind, field_index, field=field,
                              actual_publication_ns=published,
                              reference_time_ns=recorded,
                              actual=_json_value(actual[field]),
                              expected=_json_value(expected[field]), **details)
        return failed

    def _diverge(self, tally: _Tally, kind: str, field_index: int = -1,
                 **details) -> None:
        # The earliest observation first, then the contract's Channel and
        # field order; a missing or ambiguous Message sorts before its fields.
        tally.add((self.observation_ns, self.index, field_index), {
            "kind": kind, "channel": self.channel.name,
            "reference_channel": self.channel.reference_channel,
            "field": None, "observation_ns": self.observation_ns,
            "actual_publication_ns": _times_of(self.actual),
            "reference_time_ns": _times_of(self.reference),
            "actual": _values_of(self.actual, self.rules),
            "expected": _values_of(self.reference, self.rules),
            "tolerance": None, "abs_error": None, **details,
        })


def _judge(actual, expected, rule) -> tuple[str | None, dict]:
    """The divergence kind of one value pair, or None when it passes."""
    if rule == EXACT:
        return (None if actual == expected else "value"), {"tolerance": EXACT}
    allowed = rule.allowed(expected) if math.isfinite(expected) else math.inf
    tolerance = {"atol": rule.atol, "rtol": rule.rtol,
                 "allowed": _json_value(allowed)}
    if not (math.isfinite(actual) and math.isfinite(expected)):
        return "nonfinite", {"tolerance": tolerance}
    error = abs(actual - expected)
    if error <= allowed:
        return None, {}
    return "value", {"tolerance": tolerance, "abs_error": _json_value(error)}


def _times_of(messages: list):
    """One recorded time, a list of them where a side is ambiguous, or None."""
    if not messages:
        return None
    times = [t for t, _ in messages]
    return times[0] if len(times) == 1 else times


def _values_of(messages: list, rules: list):
    """The compared fields of one Message, a list where ambiguous, or None."""
    values = [{f: _json_value(v[f]) for f, _ in rules} for _, v in messages]
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def _json_value(value):
    """A value as strict JSON states it: a non-finite float becomes a string."""
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    return value


def _coverage(channel: ChannelContract, actual: dict | None,
              reference: dict | None) -> list[str]:
    """Why the contract and the two schemas of one Channel do not fit."""
    problems = []
    if actual is None:
        problems.append(f"actual Recording has no Channel {channel.name!r}")
    if reference is None:
        problems.append(
            f"reference Recording has no Channel {channel.reference_channel!r} "
            f"(compared with actual {channel.name!r})"
        )
    if problems:
        return problems
    declared = {f["name"]: f for f in actual["fields"]}
    reference_fields = {f["name"]: f for f in reference["fields"]}
    context = f"Channel {channel.name!r}"
    for name in declared:
        if name not in channel.rules:
            problems.append(
                f"{context} does not name field {name!r}; give it a rule or "
                f"'ignore'"
            )
    for name, rule in channel.rules.items():
        field = declared.get(name)
        if field is None:
            problems.append(
                f"{context} names field {name!r}, which its schema does not "
                f"declare"
            )
        elif rule != IGNORE:
            problems += _rule_problems(context, channel.reference_channel,
                                       field, reference_fields.get(name), rule)
    if not problems and not channel.compared():
        problems.append(f"{context} compares no field")
    return problems


def _rule_problems(context: str, reference_channel: str, field: dict,
                   reference: dict | None, rule) -> list[str]:
    name, kind = field["name"], field["type"]
    if "count" in field:
        return [f"{context} field {name!r} is an array; version "
                f"{CONTRACT_VERSION} compares scalar fields only, so mark it "
                f"'ignore'"]
    integer = kind in INT_RANGES
    if rule == EXACT and not integer:
        return [f"{context}: 'exact' does not fit {kind} field {name!r}; a "
                f"float field takes a tolerance"]
    if isinstance(rule, Tolerance) and integer:
        return [f"{context}: a tolerance does not fit {kind} field {name!r}; "
                f"an integer field is 'exact'"]
    if reference is None:
        return [f"reference Channel {reference_channel!r} has no field {name!r}"]
    if "count" in reference or (reference["type"] in INT_RANGES) != integer:
        return [f"reference Channel {reference_channel!r} field {name!r} is "
                f"not a scalar of the actual field's kind {kind}"]
    return []


def _schemas(path: Path) -> dict[str, dict]:
    try:
        return read_schemas(path)
    except (UnknownRecordingFormat, OSError, McapError, ValueError) as error:
        raise RecordingError(f"cannot read Recording {str(path)!r}: {error}")


def _messages(path: Path, schemas: dict,
              plans: list[tuple[ChannelContract, str, int]]
              ) -> dict[str, dict[int, list]]:
    """Per compared Channel: observation time -> [(recorded time, values)].

    Each plan names the recorded Channel and its offset on this side. Only
    Messages landing on one of the Channel's observation times are kept."""
    by_topic = {}
    for channel, topic, offset in plans:
        by_topic.setdefault(topic, []).append(
            (channel.name, offset, set(channel.times_ns)))
    codecs = {topic: MessageType(topic, schemas[topic]) for topic in by_topic}
    messages = {channel.name: {} for channel, _, _ in plans}
    try:
        for topic, t, payload in read_records(path):
            for compared, offset, times in by_topic.get(topic, ()):
                observation = t + offset
                if observation in times:
                    messages[compared].setdefault(observation, []).append(
                        (t, codecs[topic].unpack(payload)))
    except (OSError, McapError, struct.error) as error:
        raise RecordingError(f"cannot read Recording {str(path)!r}: {error}")
    return messages


def _identity(path: Path) -> dict:
    return {"path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


# -- the command ----------------------------------------------------------------

def render(report: dict) -> str:
    """The report as a person reads it."""
    lines = [
        f"verdict: {report['verdict']}",
        "comparison: reference trajectory, not a determinism check "
        "(sil-check bit-compares two Recordings of one Manifest)",
    ]
    for role in ("contract", "actual", "reference"):
        lines.append(f"{role}: {report[role]['path']} "
                     f"(sha256 {report[role]['sha256']})")
    window = report["evaluation"]
    lines.append(f"evaluation: {window['from_ns']} ns to {window['to_ns']} ns")
    for name, counts in report["channels"].items():
        lines.append(
            f"  {name} against {counts['reference_channel']}: "
            f"{counts['observations']} observations, {counts['checked']} checked, "
            f"{counts['failed']} failed ({counts['nonfinite']} non-finite), missing "
            f"{counts['missing_actual']} actual / {counts['missing_reference']} reference, "
            f"ambiguous {counts['ambiguous_actual']} actual / "
            f"{counts['ambiguous_reference']} reference"
        )
    if report["coverage"]:
        lines.append("coverage:")
        lines += [f"  - {problem}" for problem in report["coverage"]]
    first = report["first_divergence"]
    if first is not None:
        lines.append(f"first divergence: {_render_divergence(first)}")
    lines.append(f"divergences: {report['divergences']}")
    return "\n".join(lines) + "\n"


def _render_divergence(d: dict) -> str:
    where = d["channel"] + (f".{d['field']}" if d["field"] else "")
    text = (f"{d['kind']} {where} at observation {d['observation_ns']} ns "
            f"(actual recorded at {d['actual_publication_ns']}, reference "
            f"{d['reference_channel']} at {d['reference_time_ns']}): "
            f"actual {d['actual']}, expected {d['expected']}")
    tolerance = d["tolerance"]
    if isinstance(tolerance, dict):
        text += (f", allowed {tolerance['allowed']} (atol {tolerance['atol']} "
                 f"+ rtol {tolerance['rtol']} * |expected|)")
    elif tolerance == EXACT:
        text += ", exact"
    if d["abs_error"] is not None:
        text += f", abs error {d['abs_error']}"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Compare a Recording against a reference Recording under "
                    "an explicit observation contract.",
        allow_abbrev=False,
    )
    parser.add_argument("contract", type=Path,
                        help="the comparison contract (sil_comparison 1)")
    parser.add_argument("actual", type=Path, help="the Recording under test")
    parser.add_argument("reference", type=Path,
                        help="the Recording holding the expected values")
    parser.add_argument("--json", action="store_true",
                        help="write the report as JSON")
    args = parser.parse_args(argv)
    try:
        report = compare(read_contract(args.contract), args.actual,
                         args.reference)
    except (ContractError, RecordingError) as error:
        sys.stderr.write(f"{PROG}: error: {error}\n")
        return EXIT_USAGE
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    else:
        sys.stdout.write(render(report))
    return EXIT_FAIL if report["verdict"] == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
