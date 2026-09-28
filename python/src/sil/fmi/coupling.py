"""`sil-fmu-couple`: author a Run of FMUs coupled through Channels.

A coupling document names each FMU as its own Process participant and each
connection as a field of an ordinary Channel. This command turns it and
the FMU archives into an ordinary canonical Manifest. It adds no contract of
its own: each FMU's command is the Importer's, `python -m sil.fmi`, with the
FMU path as an argument of its own and one `--bind` and `--start` per choice.

    {"sil_fmu_coupling": 1, "duration_ns": 5000000000,
     "fmus": {
       "<fmu>": {"step_period_ns": 10000000, "priority": 0,
                 "start": [{"variable": "...", "value": "...", "unit": "m"}],
                 "hold": ["<input>"]}},
     "channels": {
       "<channel>": {
         "publisher": "<fmu>", "latency_ns": 10000000,
         "fields": [{"name": "...", "type": "f64", "variable": "<output>",
                     "unit": "m"}],
         "subscribers": {
           "<fmu>": {"capacity": 2, "overflow": "fail",
                     "bind": {"<field>": "<input>"}}}}}}

Nothing is inferred and nothing has a default. A Channel's schema has the
Channel's name. One connection is one field of one Channel: it carries the
publisher's output to the input each subscriber binds to the field.

Everything is checked before a Manifest is written, and nothing is loaded:

* each FMU and its mapping get the inspection's verdict, which is the verdict
  the Importer reaches when it initializes;
* each connection starts at an output of its publisher and ends at an input
  of its subscriber, and both ends declare one type, one dimension and one
  unit. The Importer converts no unit, so a unit mismatch needs an explicit
  conversion between the two FMUs;
* each input is fed once: it is connected, given a start value or held at
  the start the FMU declares. A held input and a connected input both have
  a start value, because a connected input holds it until its first
  delivery;
* each Channel states its Latency and each route states its bound. A route
  that must hold more Messages than its capacity under the `fail` policy is
  refused;
* each FMU states its own priority, so a name never decides the order of a
  Slot. A zero-Latency connection delivers inside the publishing Slot, so its
  publisher runs first. The zero-Latency connections have to be acyclic, and a
  cycle is refused with the path it takes.
* the Duration is a multiple of each FMU's period, so each FMU's last Step
  ends on the Duration. `sil-run` advances every Step by one full period and
  does not clip the last one.

The zero-Latency check is a conservative authoring profile. It reads the
declared connections only. It does not find or solve an algebraic loop in the
models' equations, and it refuses a structural cycle even where the models
would not form one. A feedback loop with a Latency above zero on at least one
Channel is an ordinary feedback loop and stays supported.

Each FMU initializes alone, from its start values, before the first Slot.
Nothing is delivered during initialization and no initial value is solved
jointly, so a connected input holds its start value until its first
delivery.

The plan states, for each FMU, its place in a Slot, its period and its Steps,
and, for each route, what a Message holds, when it is delivered, where the
start value comes from and which input each activation steps on. For each
Channel it states who takes the last Message, which holds the values at the
Duration; one that no activation takes is only in the Recording. The receipt
holds the plan and the digests of the document, each FMU and the Manifest.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from sil import build_info
from sil.fmi.documents import (
    AuthoringError,
    array,
    exact_object,
    file_sha256,
    load,
    read,
    require_distinct,
    sha256,
    string,
    unit,
)
from sil.fmi.inspection import COMPATIBLE, MAPPING_VERSION, inspect
from sil.manifest import Manifest, ManifestError, SubscriberRoute

DOCUMENT_VERSION = 1
RECEIPT_VERSION = 1
PROG = "sil-fmu-couple"

_DOCUMENT_KEYS = {"sil_fmu_coupling", "duration_ns", "fmus", "channels"}
_FMU_KEYS = {"step_period_ns", "priority", "start", "hold"}
_CHANNEL_KEYS = {"publisher", "latency_ns", "fields", "subscribers"}
_FIELD_KEYS = {"name", "type", "variable", "unit"}
_ROUTE_KEYS = {"capacity", "overflow", "bind"}
# The causalities FMI 3.0 lets an importer set before initialization.
_STARTABLE = ("input", "parameter", "structuralParameter")
# How many deliveries of each route the plan lists.
_SHOWN_DELIVERIES = 3
# The most activations of each route the plan lists.
_SHOWN_ACTIVATIONS = 12


@dataclass(frozen=True)
class Connection:
    """One field of one Channel, from its publisher's output to one input."""

    channel: str
    field: str
    publisher: str
    output: str
    subscriber: str
    input: str
    unit: str | None

    def __str__(self) -> str:
        return (f"Channel {self.channel!r} field {self.field!r} "
                f"({self.publisher}.{self.output} -> "
                f"{self.subscriber}.{self.input})")


def couple(document: str | Path, fmus: dict[str, str | Path],
           out: str | Path) -> dict:
    """Write the Manifest for `document` over the `fmus` archives to `out`;
    return the receipt. Nothing is written when the Run is rejected."""
    document, out = Path(document), Path(out)
    fmus = {name: Path(path) for name, path in fmus.items()}
    require_distinct(out, "Manifest", {
        "document": document,
        **{f"FMU {name!r}": path for name, path in fmus.items()},
    })
    document_bytes = read(document, "coupling document")
    doc = _parse(document_bytes, document)
    _require_archives(doc, fmus)
    manifest = _manifest(doc, fmus)
    _require_whole_steps(doc)
    connections = _connections(doc)
    reports = {name: _inspected(name, fmus[name], doc) for name in doc["fmus"]}
    variables = {name: {v["name"]: v for v in report["variables"]}
                 for name, report in reports.items()}
    for connection in connections:
        _require_compatible(connection, variables)
    for name in doc["fmus"]:
        _require_sources(name, doc, connections, variables[name])
    for name, report in reports.items():
        _require_mapping_accepted(name, fmus[name], report)
    order = _execution_order(doc)
    plan = _plan(doc, order, variables)
    _require_route_bounds(plan)
    text = manifest.to_json()
    try:
        out.write_text(text)
    except OSError as e:
        raise AuthoringError(f"cannot write Manifest {str(out)!r}: {e}") from e
    return _receipt(connections, variables, plan, {
        "document": {"file": document.name, "sha256": sha256(document_bytes)},
        "fmus": {
            name: {"file": fmus[name].name, "sha256": file_sha256(fmus[name]),
                   "model_name": report["facts"]["model_name"],
                   "instantiation_token":
                       report["facts"]["instantiation_token"]}
            for name, report in sorted(reports.items())
        },
        "manifest": {"file": out.name, "sha256": sha256(text.encode())},
    })


def _receipt(connections: list[Connection], variables: dict, plan: dict,
             files: dict) -> dict:
    return {
        "sil_fmu_coupling_receipt": RECEIPT_VERSION,
        "author": {"name": PROG, "version": build_info.__version__,
                   "revision": build_info.SOURCE_REVISION},
        **files,
        "connections": [
            {"channel": c.channel, "field": c.field,
             "publisher": c.publisher, "output": c.output,
             "subscriber": c.subscriber, "input": c.input,
             "type": variables[c.publisher][c.output]["type"],
             "unit": c.unit}
            for c in connections
        ],
        "plan": plan,
    }


# Document ----------------------------------------------------------------------


def _parse(data: bytes, path: Path) -> dict:
    doc = load(data, path, "coupling document")
    try:
        return _document(doc)
    except AuthoringError as e:
        raise AuthoringError(f"coupling document {str(path)!r}: {e}") from None


def _document(doc) -> dict:
    doc = exact_object(doc, "the document", _DOCUMENT_KEYS)
    version = doc["sil_fmu_coupling"]
    if isinstance(version, bool) or version != DOCUMENT_VERSION:
        raise AuthoringError(
            f"'sil_fmu_coupling' must be {DOCUMENT_VERSION}, got {version!r}"
        )
    for key in ("fmus", "channels"):
        if not isinstance(doc[key], dict) or not doc[key]:
            raise AuthoringError(f"{key!r} must be a non-empty object")
    for name, fmu in doc["fmus"].items():
        _fmu(name, fmu)
    for name, channel in doc["channels"].items():
        _channel(name, channel, doc["fmus"])
    return doc


def _fmu(name: str, fmu) -> None:
    context = f"FMU {name!r}"
    string(name, "an FMU name")
    exact_object(fmu, context, _FMU_KEYS)
    for index, start in enumerate(array(fmu["start"], f"{context} 'start'")):
        where = f"{context} start[{index}]"
        exact_object(start, where, {"variable", "value", "unit"})
        string(start["variable"], f"{where} 'variable'")
        string(start["value"], f"{where} 'value'")
        unit(start["unit"], f"{where} 'unit'")
    hold = array(fmu["hold"], f"{context} 'hold'")
    for index, variable in enumerate(hold):
        string(variable, f"{context} hold[{index}]")
    if len(set(hold)) != len(hold):
        raise AuthoringError(f"{context} 'hold' names a variable twice")


def _channel(name: str, channel, fmus: dict) -> None:
    context = f"channel {name!r}"
    exact_object(channel, context, _CHANNEL_KEYS)
    latency = channel["latency_ns"]
    if not isinstance(latency, int) or isinstance(latency, bool):
        raise AuthoringError(
            f"{context} 'latency_ns' must be an integer, got {latency!r}; "
            f"every connection states its Latency, and 0 is the one that "
            f"delivers inside the publishing Slot"
        )
    _declared_fmu(channel["publisher"], fmus, f"{context} names publisher")
    fields = array(channel["fields"], f"{context} 'fields'")
    if not fields:
        raise AuthoringError(f"{context} 'fields' must be a non-empty array")
    for index, field in enumerate(fields):
        where = f"{context} field[{index}]"
        exact_object(field, where, _FIELD_KEYS)
        for key in ("name", "type", "variable"):
            string(field[key], f"{where} {key!r}")
        unit(field["unit"], f"{where} 'unit'")
    names = {field["name"] for field in fields}
    if not isinstance(channel["subscribers"], dict):
        raise AuthoringError(f"{context} 'subscribers' must be an object")
    for subscriber, route in channel["subscribers"].items():
        _declared_fmu(subscriber, fmus, f"{context} names subscriber")
        where = f"{context} subscriber {subscriber!r}"
        exact_object(route, where, _ROUTE_KEYS)
        bind = route["bind"]
        if not isinstance(bind, dict) or not bind:
            raise AuthoringError(f"{where} 'bind' must be a non-empty object")
        for field, variable in bind.items():
            if field not in names:
                raise AuthoringError(
                    f"{where} binds field {field!r}, which Channel {name!r} "
                    f"does not carry (fields: "
                    f"{', '.join(map(repr, sorted(names)))})"
                )
            string(variable, f"{where} bind {field!r}")


def _declared_fmu(name, fmus: dict, what: str) -> None:
    if name not in fmus:
        raise AuthoringError(
            f"{what} {name!r}, which 'fmus' does not declare (declared: "
            f"{', '.join(map(repr, fmus))})"
        )


def _require_archives(doc: dict, fmus: dict[str, Path]) -> None:
    for name in doc["fmus"]:
        if name not in fmus:
            raise AuthoringError(
                f"FMU {name!r} has no archive; name it with --fmu {name} <path>"
            )
    for name in fmus:
        if name not in doc["fmus"]:
            raise AuthoringError(
                f"an archive is given for {name!r}, and the document declares "
                f"no FMU of that name"
            )


def _connections(doc: dict) -> list[Connection]:
    """Every connection, in the order the Manifest states them."""
    connections = []
    for channel in sorted(doc["channels"]):
        declaration = doc["channels"][channel]
        for field in declaration["fields"]:
            for subscriber in sorted(declaration["subscribers"]):
                bind = declaration["subscribers"][subscriber]["bind"]
                if field["name"] in bind:
                    connections.append(Connection(
                        channel, field["name"], declaration["publisher"],
                        field["variable"], subscriber, bind[field["name"]],
                        field["unit"],
                    ))
    return connections


# The Manifest ------------------------------------------------------------------


def _bind_arguments(doc: dict, name: str) -> list[str]:
    """One FMU's bindings: the Channels it takes, then those it publishes."""
    channels = doc["channels"]
    arguments = []
    for channel in _subscribed(doc, name):
        bind = channels[channel]["subscribers"][name]["bind"]
        arguments += [f"{channel}:{field['name']}={bind[field['name']]}"
                      for field in channels[channel]["fields"]
                      if field["name"] in bind]
    for channel in _published(doc, name):
        arguments += [f"{channel}:{field['name']}={field['variable']}"
                      for field in channels[channel]["fields"]]
    return arguments


def _start_arguments(doc: dict, name: str) -> list[str]:
    starts = sorted(doc["fmus"][name]["start"], key=lambda s: s["variable"])
    return [f"{s['variable']}={s['value']}" for s in starts]


def _subscribed(doc: dict, name: str) -> list[str]:
    return sorted(channel for channel, declaration in doc["channels"].items()
                  if name in declaration["subscribers"])


def _published(doc: dict, name: str) -> list[str]:
    return sorted(channel for channel, declaration in doc["channels"].items()
                  if declaration["publisher"] == name)


def _schemas(doc: dict) -> dict:
    return {channel: {"fields": [{"name": f["name"], "type": f["type"]}
                                 for f in declaration["fields"]]}
            for channel, declaration in doc["channels"].items()}


def _manifest(doc: dict, fmus: dict[str, Path]) -> Manifest:
    """The canonical Manifest of the coupled Run.

    Every list in it is sorted, so the order in which the document declares
    FMUs, Channels, subscribers and start values does not change its bytes.
    """
    try:
        manifest = Manifest(duration_ns=doc["duration_ns"])
        manifest.add_schemas(_schemas(doc))
        for channel in sorted(doc["channels"]):
            manifest.add_channel(
                channel, schema=channel,
                latency_ns=doc["channels"][channel]["latency_ns"])
        for name in sorted(doc["fmus"]):
            fmu = doc["fmus"][name]
            command = ["python3", "-m", "sil.fmi", str(fmus[name].resolve())]
            for argument in _bind_arguments(doc, name):
                command += ["--bind", argument]
            for argument in _start_arguments(doc, name):
                command += ["--start", argument]
            manifest.add_process(
                name, command=command, step_period_ns=fmu["step_period_ns"],
                subscribes=[
                    SubscriberRoute(channel, **{
                        key: doc["channels"][channel]["subscribers"][name][key]
                        for key in ("capacity", "overflow")})
                    for channel in _subscribed(doc, name)
                ],
                publishes=_published(doc, name),
                priority=fmu["priority"],
            )
        # The builder checks participant routes and Channels only here, and
        # a rejection must come before anything is written.
        manifest.to_doc()
    except ManifestError as e:
        raise AuthoringError(str(e)) from None
    return manifest


def _require_whole_steps(doc: dict) -> None:
    """Each FMU's last Step ends on the Duration.

    The kernel starts a Step at each Slot below the Duration and always
    advances it by one full period; it does not clip the last Step. A
    Duration that is not a multiple of a period would step that FMU past
    the Duration, so this profile refuses it.
    """
    duration = doc["duration_ns"]
    periods = {name: doc["fmus"][name]["step_period_ns"]
               for name in sorted(doc["fmus"])}
    split = [name for name, period in periods.items() if duration % period]
    if not split:
        return
    first, period = split[0], periods[split[0]]
    last = duration - duration % period
    common = math.lcm(*periods.values())
    below = duration - duration % common
    such_as = " or ".join(_time(ns) for ns in (below, below + common) if ns)
    raise AuthoringError(
        f"Duration {_time(duration)} is not a multiple of the period of "
        + ", ".join(f"FMU {name!r} (period {_time(periods[name])})"
                    for name in split)
        + f". The last Step of {first!r} would start at {_time(last)} and end "
        f"at {_time(last + period)}, after the Duration. sil-run advances "
        f"every Step by one full period and does not clip the last one, so "
        f"this profile requires a Duration that is a multiple of every "
        f"period ({_time(common)}), such as {such_as}"
    )


# The FMUs ----------------------------------------------------------------------


def _inspected(name: str, fmu: Path, doc: dict) -> dict:
    """The inspection of one FMU and of the mapping its command carries."""
    channels = {c: {"schema": c, "direction": "in"}
                for c in _subscribed(doc, name)}
    channels.update({c: {"schema": c, "direction": "out"}
                     for c in _published(doc, name)})
    schemas = _schemas(doc)
    report = inspect(fmu, {
        "sil_fmi_mapping": MAPPING_VERSION,
        "schemas": {c: schemas[c] for c in channels},
        "channels": channels,
        "bind": _bind_arguments(doc, name),
        "start": _start_arguments(doc, name),
    })
    if report["unusable"]:
        raise AuthoringError(
            f"the Importer cannot drive FMU {name!r} ({str(fmu)!r}): "
            + "; ".join(report["unusable"])
        )
    return report


def _require_mapping_accepted(name: str, fmu: Path, report: dict) -> None:
    if report["verdict"] != COMPATIBLE:
        raise AuthoringError(
            f"FMU {name!r} ({str(fmu)!r}) rejects the mapping: "
            f"{report['mapping']['rejection']}"
        )


def _variable(variables: dict[str, dict], fmu: str, name: str,
              what: str) -> dict:
    """One variable of one FMU, which `what` names."""
    variable = variables.get(name)
    if variable is None:
        raise AuthoringError(
            f"{what} names variable {name!r}, which FMU {fmu!r} does not "
            f"declare"
        )
    return variable


def _require_compatible(connection: Connection, variables: dict) -> None:
    """Both ends of one connection: an output and an input that agree."""
    output = _variable(variables[connection.publisher], connection.publisher,
                       connection.output, str(connection))
    taken = _variable(variables[connection.subscriber],
                      connection.subscriber, connection.input,
                      str(connection))
    if output["causality"] != "output":
        raise AuthoringError(
            f"{connection} starts at {connection.output!r}, whose causality "
            f"is {output['causality']!r}; a connection starts at an output "
            f"of its publisher"
        )
    if taken["causality"] != "input":
        raise AuthoringError(
            f"{connection} ends at {connection.input!r}, whose causality is "
            f"{taken['causality']!r}; a connection ends at an input of its "
            f"subscriber"
        )
    for key, what in (("type", "type"), ("dimensions", "dimensions")):
        if output[key] != taken[key]:
            raise AuthoringError(
                f"{connection} connects {what} {output[key]} to {what} "
                f"{taken[key]}; both ends of a connection declare one {what}"
            )
    if connection.unit != output["unit"]:
        stated = ("states no unit" if connection.unit is None
                  else f"is stated in {connection.unit!r}")
        declared = ("declares no unit; state its unit as null"
                    if output["unit"] is None
                    else f"is in {output['unit']!r}; state that unit")
        raise AuthoringError(
            f"{connection} {stated}, and output {connection.output!r} of "
            f"{connection.publisher!r} {declared}"
        )
    if taken["unit"] != output["unit"]:
        raise AuthoringError(
            f"{connection} connects {output['unit']!r} to "
            f"{taken['unit']!r}. The Importer converts no unit, so a "
            f"connection joins variables of one unit: author the conversion "
            f"explicitly, as a converting FMU of this document between the "
            f"two"
        )


def _require_sources(name: str, doc: dict, connections: list[Connection],
                     variables: dict[str, dict]) -> None:
    """Each input of one FMU is fed once, and each connected one has a
    start value to hold until its first delivery."""
    what = f"FMU {name!r}"
    fed: dict[str, Connection] = {}
    for connection in connections:
        if connection.subscriber != name:
            continue
        first = fed.get(connection.input)
        if first is not None:
            raise AuthoringError(
                f"{what} input {connection.input!r} is fed by {first} and by "
                f"{connection}; an input is fed by one connection"
            )
        fed[connection.input] = connection
    started = _require_starts(name, doc["fmus"][name]["start"], variables)
    _require_holds(name, doc["fmus"][name]["hold"], variables, fed, started)
    for variable in fed:
        if variables[variable]["start"] is None and variable not in started:
            raise AuthoringError(
                f"{what} input {variable!r} is connected and has no start "
                f"value; it holds one until its first delivery, so give it "
                f"one in 'start'"
            )
    declared = set(fed) | set(started) | set(doc["fmus"][name]["hold"])
    declared |= {clock for v in fed for clock in variables[v]["clocks"]}
    missing = [v["name"] for v in variables.values()
               if v["causality"] == "input" and v["name"] not in declared]
    if missing:
        raise AuthoringError(
            f"{what} input {', '.join(map(repr, missing))} is neither "
            f"connected, started nor held; connect it, give it a start value, "
            f"or hold it at its declared start"
        )


def _require_starts(fmu: str, starts: list[dict],
                    variables: dict[str, dict]) -> set[str]:
    what = f"FMU {fmu!r}"
    started = set()
    for start in starts:
        name = start["variable"]
        variable = _variable(variables, fmu, name, f"{what} start value")
        if variable["causality"] not in _STARTABLE:
            raise AuthoringError(
                f"{what} start value for {name!r}: its causality is "
                f"{variable['causality']!r}; an importer sets only an input "
                f"or a parameter before initialization"
            )
        if start["unit"] != variable["unit"]:
            raise AuthoringError(
                f"{what} start value for {name!r} is stated in "
                f"{start['unit']!r}, and the variable is in "
                f"{variable['unit']!r}; state the value in its unit"
            )
        if name in started:
            raise AuthoringError(f"{what} starts {name!r} twice")
        started.add(name)
    return started


def _require_holds(fmu: str, hold: list[str], variables: dict[str, dict],
                   fed: dict, started: set[str]) -> None:
    what = f"FMU {fmu!r}"
    for name in hold:
        variable = _variable(variables, fmu, name, f"{what} 'hold'")
        if variable["causality"] != "input":
            raise AuthoringError(
                f"{what} holds {name!r}, whose causality is "
                f"{variable['causality']!r}; only an input is held at its "
                f"declared start"
            )
        if name in fed or name in started:
            also = "connected" if name in fed else "given a start value"
            raise AuthoringError(f"{what} holds {name!r}, which is also {also}")
        if variable["start"] is None:
            raise AuthoringError(
                f"{what} holds {name!r}, which declares no start value; give "
                f"it one in 'start'"
            )


# Execution order ---------------------------------------------------------------


def _same_slot(doc: dict) -> list[tuple[str, str, str]]:
    """Each zero-Latency route, as (publisher, subscriber, Channel)."""
    return sorted(
        (declaration["publisher"], subscriber, channel)
        for channel, declaration in doc["channels"].items()
        if declaration["latency_ns"] == 0
        for subscriber in declaration["subscribers"]
    )


def _execution_order(doc: dict) -> list[str]:
    """The FMUs in the order each Slot activates them.

    The order is the authored priorities'. It is checked here, and never
    chosen: a same-Slot ordering change is an authored change.
    """
    fmus = doc["fmus"]
    by_priority: dict[int, str] = {}
    for name in sorted(fmus):
        other = by_priority.setdefault(fmus[name]["priority"], name)
        if other != name:
            raise AuthoringError(
                f"FMUs {other!r} and {name!r} both declare priority "
                f"{fmus[name]['priority']}; each FMU states its own place in "
                f"a Slot, so that no name decides the order"
            )
    edges = _same_slot(doc)
    cycle = _cycle(sorted(fmus), edges)
    if cycle:
        raise AuthoringError(
            f"the zero-Latency connections form a cycle: {cycle}. A Latency "
            f"of 0 delivers inside the publishing Slot, so its publisher runs "
            f"first, and a cycle has no first FMU. Declare a Latency above 0 "
            f"on one of its Channels. This authoring profile checks the "
            f"declared connections only"
        )
    for publisher, subscriber, channel in edges:
        if fmus[publisher]["priority"] >= fmus[subscriber]["priority"]:
            raise AuthoringError(
                f"Channel {channel!r} has Latency 0, so {publisher!r} "
                f"(priority {fmus[publisher]['priority']}) must run before "
                f"{subscriber!r} (priority {fmus[subscriber]['priority']}) "
                f"in a Slot. Declare priorities in an order such as: "
                f"{', '.join(_topological(fmus, edges))}"
            )
    return sorted(fmus, key=lambda name: fmus[name]["priority"])


def _cycle(nodes: list[str], edges: list[tuple[str, str, str]]) -> str | None:
    """The first cycle a depth-first search meets, spelled with its Channels."""
    successors: dict[str, list[tuple[str, str]]] = {n: [] for n in nodes}
    for publisher, subscriber, channel in edges:
        successors[publisher].append((subscriber, channel))
    done: set[str] = set()

    def visit(node: str, path: list[tuple[str, str]]) -> str | None:
        on_path = [n for n, _ in path]
        if node in on_path:
            loop = path[on_path.index(node):]
            return "".join(f"{n} -[{c}]-> " for n, c in loop) + node
        if node in done:
            return None
        for successor, channel in successors[node]:
            found = visit(successor, path + [(node, channel)])
            if found:
                return found
        done.add(node)
        return None

    for node in nodes:
        found = visit(node, [])
        if found:
            return found
    return None


def _topological(fmus: dict, edges: list[tuple[str, str, str]]) -> list[str]:
    """An order the zero-Latency connections allow, nearest the authored one."""
    waiting = {name: {p for p, s, _ in edges if s == name} for name in fmus}
    order: list[str] = []
    while waiting:
        ready = [n for n, before in waiting.items() if not before - set(order)]
        chosen = min(ready, key=lambda n: (fmus[n]["priority"], n))
        order.append(chosen)
        del waiting[chosen]
    return order


# The plan ----------------------------------------------------------------------


def _plan(doc: dict, order: list[str], variables: dict) -> dict:
    fmus = doc["fmus"]
    routes = [
        _route(doc, channel, subscriber, variables[subscriber])
        for channel in sorted(doc["channels"])
        for subscriber in sorted(doc["channels"][channel]["subscribers"])
    ]
    return {
        "duration_ns": doc["duration_ns"],
        "order": [{"fmu": name, "priority": fmus[name]["priority"],
                   "step_period_ns": fmus[name]["step_period_ns"],
                   "steps": doc["duration_ns"] // fmus[name]["step_period_ns"]}
                  for name in order],
        "same_slot": [{"channel": c, "publisher": p, "subscriber": s}
                      for p, s, c in _same_slot(doc)],
        "channels": [
            {"channel": channel,
             "publisher": doc["channels"][channel]["publisher"],
             "latency_ns": doc["channels"][channel]["latency_ns"],
             "fields": [{"name": f["name"], "output": f["variable"],
                         "unit": f["unit"]}
                        for f in doc["channels"][channel]["fields"]],
             "final": _final(doc, channel, routes)}
            for channel in sorted(doc["channels"])
        ],
        "routes": routes,
    }


def _final(doc: dict, channel: str, routes: list[dict]) -> dict:
    """The last Message of one Channel: the one that holds the values its
    publisher reaches at the Duration, and who takes it within the Run."""
    period = doc["fmus"][doc["channels"][channel]["publisher"]][
        "step_period_ns"]
    return {
        "published_ns": doc["duration_ns"] - period,
        "values_at_ns": doc["duration_ns"],
        "taken_by": {r["subscriber"]: r["final_taken_ns"] for r in routes
                     if r["channel"] == channel
                     and r["final_taken_ns"] is not None},
    }


def _route(doc: dict, channel: str, subscriber: str,
           variables: dict[str, dict]) -> dict:
    """One route: its bound, its deliveries, what it holds before them, and
    the input each activation steps on."""
    declaration = doc["channels"][channel]
    publisher = declaration["publisher"]
    route = declaration["subscribers"][subscriber]
    fmus = doc["fmus"]
    timeline = _timeline(
        fmus[publisher]["step_period_ns"], fmus[subscriber]["step_period_ns"],
        declaration["latency_ns"], doc["duration_ns"],
        publisher_first=fmus[publisher]["priority"]
        < fmus[subscriber]["priority"],
        drop_above=route["capacity"] if route["overflow"] == "drop_newest"
        else None,
    )
    starts = {s["variable"]: {"value": s["value"], "from": "document"}
              for s in fmus[subscriber]["start"]}
    return {
        "channel": channel, "publisher": publisher, "subscriber": subscriber,
        "latency_ns": declaration["latency_ns"],
        "capacity": route["capacity"], "overflow": route["overflow"],
        **timeline,
        "until_first_delivery": {
            variable: starts.get(variable, {
                "value": variables[variable]["start"], "from": "FMU"})
            for variable in route["bind"].values()
        },
    }


def _timeline(publisher_period: int, subscriber_period: int, latency: int,
              duration: int, *, publisher_first: bool,
              drop_above: int | None) -> dict:
    """The deliveries and drops of one route, the most Messages it holds, and
    the input each activation of the subscriber steps on.

    A Message is published in each of the publisher's Slots, holding the
    values the publisher reaches one Period later. It enters the route when
    it is published and leaves it at the first activation of the subscriber
    at or after its visible time, in Publish order. In a Slot both share, the
    subscriber drains before a later publisher publishes. Under `drop_newest`
    the route refuses a Message that finds it holding `drop_above`; under
    `fail` it takes every Message, and the peak says whether the Run fails.

    An activation writes each Message it takes in Publish order, so it steps
    on the newest one. An activation that takes none holds the last Message
    taken, or the start value before the first. The activations are listed
    until their pattern repeats: one common period of the two FMUs after the
    first Message can be visible. A common period can be long, so at most
    `_SHOWN_ACTIVATIONS` are listed, and the rest are counted.
    """
    publications = iter(range(0, duration, publisher_period))
    final = duration - publisher_period
    pending: deque[int] = deque()
    deliveries: list[dict] = []
    activations: list[dict] = []
    not_listed = 0
    dropped: list[int] = []
    peak = 0
    held: dict | str = "start"
    final_taken = None
    shown_until = latency + math.lcm(publisher_period, subscriber_period)
    publication = next(publications, None)

    def publish(published_ns: int) -> None:
        nonlocal peak
        if drop_above is not None and len(pending) >= drop_above:
            dropped.append(published_ns)
            return
        pending.append(published_ns)
        peak = max(peak, len(pending))

    for drain in range(0, duration, subscriber_period):
        while publication is not None and (
            publication < drain or (publication == drain and publisher_first)
        ):
            publish(publication)
            publication = next(publications, None)
        taken = 0
        while pending and pending[0] + latency <= drain:
            published_ns = pending.popleft()
            taken += 1
            held = {"published_ns": published_ns,
                    "values_at_ns": published_ns + publisher_period}
            if published_ns == final:
                final_taken = drain
            if len(deliveries) < _SHOWN_DELIVERIES:
                deliveries.append({**held, "delivered_ns": drain})
        if drain < shown_until:
            if len(activations) < _SHOWN_ACTIVATIONS:
                activations.append({"at_ns": drain, "delivered": taken,
                                    "input": held})
            else:
                not_listed += 1
    while publication is not None:
        publish(publication)
        publication = next(publications, None)
    return {"peak_messages": peak, "deliveries": deliveries,
            "dropped_messages": len(dropped),
            "first_dropped_ns": dropped[:_SHOWN_DELIVERIES],
            "activations": activations, "activations_not_listed": not_listed,
            "final_taken_ns": final_taken}


def _require_route_bounds(plan: dict) -> None:
    for route in plan["routes"]:
        if route["overflow"] == "fail" and (
            route["peak_messages"] > route["capacity"]
        ):
            raise AuthoringError(
                f"Channel {route['channel']!r} holds up to "
                f"{route['peak_messages']} Messages in the route to "
                f"{route['subscriber']!r}, which declares capacity "
                f"{route['capacity']} and overflow 'fail'; the Run would fail "
                f"when it overflows. Raise the capacity, or lower the Latency"
            )


def _time(ns: int) -> str:
    for unit_ns, name in ((1_000_000, "ms"), (1_000, "us")):
        if ns % unit_ns == 0:
            return f"{ns // unit_ns} {name}"
    return f"{ns} ns"


def render_plan(plan: dict) -> str:
    """The plan as a person reads it."""
    duration = _time(plan["duration_ns"])
    lines = [f"Duration {duration}: Slots at 0 <= t < {duration}; the last "
             f"Step of each FMU ends on the Duration",
             "execution order in each Slot (lowest priority first):"]
    lines += [f"  {index}. {fmu['fmu']}  priority {fmu['priority']}, "
              f"period {_time(fmu['step_period_ns'])}, {fmu['steps']} Steps"
              for index, fmu in enumerate(plan["order"], 1)]
    same_slot = [f"{s['channel']} ({s['publisher']} before {s['subscriber']})"
                 for s in plan["same_slot"]]
    lines.append(f"same-Slot connections: {', '.join(same_slot) or 'none'}")
    lines.append("Channels (a Message published at t holds its publisher's "
                 "outputs at t + the publisher's period):")
    for channel in plan["channels"]:
        fields = ", ".join(
            f"{f['name']} = {f['output']}"
            + (f" [{f['unit']}]" if f["unit"] else "")
            for f in channel["fields"])
        lines.append(f"  {channel['channel']}, published by "
                     f"{channel['publisher']} with Latency "
                     f"{_time(channel['latency_ns'])}: {fields}")
        routes = [r for r in plan["routes"]
                  if r["channel"] == channel["channel"]]
        lines += [line for route in routes for line in _render_route(route)]
        if not routes:
            lines.append("    no subscriber; the Recording holds it")
        lines.append(_render_final(channel["final"]))
    return "\n".join(lines) + "\n"


def _render_final(final: dict) -> str:
    taken = ", ".join(f"{subscriber} takes it at {_time(ns)}"
                      for subscriber, ns in final["taken_by"].items())
    return (f"    last Message published at {_time(final['published_ns'])} "
            f"(values at {_time(final['values_at_ns'])}): "
            + (taken or "no activation takes it; only the Recording holds it"))


def _render_route(route: dict) -> list[str]:
    peak = route["peak_messages"]
    lines = [
        f"    to {route['subscriber']}: route capacity {route['capacity']} "
        f"({route['overflow']}), at most {peak} "
        f"Message{'' if peak == 1 else 's'} in the route"
    ]
    lines += [f"      {route['subscriber']}.{variable} holds {start['value']} "
              f"({start['from']} start) until the first delivery"
              for variable, start in route["until_first_delivery"].items()]
    lines += [f"      {_render_activation(a)}" for a in route["activations"]]
    if route["activations_not_listed"]:
        count = route["activations_not_listed"]
        lines.append(f"      {count} more activation{'' if count == 1 else 's'} "
                     f"before the pattern repeats "
                     f"{'is' if count == 1 else 'are'} not listed")
    if not route["deliveries"]:
        lines.append("      no Message is delivered within the Run")
    if route["dropped_messages"]:
        first = ", ".join(_time(ns) for ns in route["first_dropped_ns"])
        lines.append(f"      {route['dropped_messages']} Messages dropped, "
                     f"the first published at {first}")
    return lines


def _render_activation(activation: dict) -> str:
    at, held = _time(activation["at_ns"]), activation["input"]
    if held == "start":
        return f"at {at}: no Message yet, holds the start value"
    message = (f"published at {_time(held['published_ns'])} "
               f"(values at {_time(held['values_at_ns'])})")
    taken = activation["delivered"]
    if taken == 0:
        return f"at {at}: no new Message, holds the one {message}"
    if taken == 1:
        return f"at {at}: takes the Message {message}"
    return f"at {at}: takes {taken} Messages and steps on the newest, {message}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Write the Manifest of a Run of FMUs coupled by ordinary "
                    "Channels, from a coupling document, and print its plan.",
        allow_abbrev=False,
    )
    parser.add_argument("document", type=Path,
                        help="the coupling document (sil_fmu_coupling 1)")
    parser.add_argument("--fmu", nargs=2, action="append", required=True,
                        metavar=("NAME", "PATH"),
                        help="the archive of the FMU the document names NAME")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="the Manifest to write")
    parser.add_argument("--receipt", type=Path, default=None,
                        help="write the receipt, with the plan, here")
    args = parser.parse_args(argv)
    try:
        fmus: dict[str, Path] = {}
        for name, path in args.fmu:
            if name in fmus:
                raise AuthoringError(f"--fmu {name!r} is given twice")
            fmus[name] = Path(path)
        if args.receipt is not None:
            require_distinct(args.receipt, "receipt", {
                "manifest": args.output, "document": args.document,
                **{f"FMU {name!r}": path for name, path in fmus.items()},
            })
        receipt = couple(args.document, fmus, args.output)
    except AuthoringError as e:
        sys.stderr.write(f"{PROG}: error: {e}\n")
        return 2
    sys.stdout.write(render_plan(receipt["plan"]))
    if args.receipt is None:
        return 0
    try:
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    except OSError as e:
        sys.stderr.write(f"{PROG}: error: cannot write receipt "
                         f"{str(args.receipt)!r}: {e}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
