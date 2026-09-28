"""`sil-fmu-couple`: author a Run of FMUs coupled through Channels (#187).

A coupling document names FMUs as separate Process participants and the
Channels that connect them. Every check is made before anything runs, and the
FMU checks are the inspection's, so no archive is loaded here: the ACC
archives carry an empty binary, and the `Feedthrough` ones are never stepped.
The Runs at the end step `Feedthrough` itself.
"""

from __future__ import annotations

import copy
import ctypes
import json
import struct
import sys
import zipfile
from pathlib import Path

import pytest
from conftest import ROOT, load_module, run_manifest

from sil.fmi import library_suffix, platform_directory
from sil.fmi.coupling import AuthoringError, couple, main, render_plan
from sil.recording import read_records

EXAMPLE = ROOT / "examples" / "fmu-coupling"
FEEDTHROUGH = ROOT / "tests" / "fixtures" / "reference-fmus" / "3.0" / "Feedthrough.fmu"
ACC_PROOF = ROOT / "proofs" / "acc-fmi"
MS = 1_000_000

ACC = json.loads((EXAMPLE / "acc.json").read_text())
FEEDBACK = json.loads((EXAMPLE / "feedback.json").read_text())


def edited(document: dict, edit) -> dict:
    document = copy.deepcopy(document)
    edit(document)
    return document


def write(tmp_path: Path, document: dict, name: str = "coupling.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document))
    return path


def feedthroughs(tmp_path: Path, document: dict = FEEDBACK) -> dict[str, Path]:
    return {name: FEEDTHROUGH for name in document["fmus"]}


def rejection(tmp_path: Path, document: dict, fmus: dict[str, Path] | None = None
              ) -> str:
    out = tmp_path / "manifest.json"
    with pytest.raises(AuthoringError) as raised:
        couple(write(tmp_path, document),
               fmus or feedthroughs(tmp_path, document), out)
    assert not out.exists()
    return str(raised.value)


def with_units(tmp_path: Path, name: str, units: dict[str, str]) -> Path:
    """`Feedthrough` with a unit declared on some of its Float64 variables."""
    archive = tmp_path / f"{name}.fmu"
    with zipfile.ZipFile(FEEDTHROUGH) as source, \
            zipfile.ZipFile(archive, "w") as target:
        for member in source.infolist():
            data = source.read(member)
            if member.filename == "modelDescription.xml":
                for variable, unit in units.items():
                    data = data.replace(
                        f'name="{variable}" '.encode(),
                        f'name="{variable}" unit="{unit}" '.encode(),
                    )
            target.writestr(member, data)
    return archive


# The ACC archives -------------------------------------------------------------

@pytest.fixture(scope="module")
def contract():
    """The #148 proof's own Manifest authoring, loaded without its runner."""
    sys.path.insert(0, str(ACC_PROOF))
    try:
        return load_module("acc_loop_contract", ACC_PROOF / "loop_contract.py")
    finally:
        sys.path.remove(str(ACC_PROOF))


def _variable(name: str, reference: int, causality: str, unit: str,
              start: float | None) -> str:
    start_attribute = "" if start is None else f' start="{start}"'
    initial = ' initial="calculated"' if causality == "output" else ""
    return (f'<Float64 name="{name}" valueReference="{reference}" '
            f'causality="{causality}" unit="{unit}"{start_attribute}'
            f'{initial}/>')


def acc_archive(tmp_path: Path, model: str, inputs: dict, outputs: dict) -> Path:
    """An archive declaring what `proofs/acc-fmi/models/<model>.py` declares.

    Its binary is empty: authoring reads the description and never loads it.
    """
    variables = ['<Float64 name="time" valueReference="0" '
                 'causality="independent" variability="continuous" unit="s"/>']
    references = iter(range(1, 100))
    output_references = []
    for name, (unit, start) in inputs.items():
        variables.append(_variable(name, next(references), "input", unit, start))
    for name, unit in outputs.items():
        reference = next(references)
        output_references.append(reference)
        variables.append(_variable(name, reference, "output", unit, None))
    structure = "".join(f'<Output valueReference="{r}"/>'
                        for r in output_references)
    description = f"""<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="3.0" modelName="{model}"
  instantiationToken="{{00000000-0000-0000-0000-000000000000}}">
  <CoSimulation modelIdentifier="{model}"
    canHandleVariableCommunicationStepSize="false"/>
  <UnitDefinitions>
    <Unit name="s"><BaseUnit s="1"/></Unit>
    <Unit name="m"><BaseUnit m="1"/></Unit>
    <Unit name="m/s"><BaseUnit m="1" s="-1"/></Unit>
    <Unit name="m/s2"><BaseUnit m="1" s="-2"/></Unit>
  </UnitDefinitions>
  <ModelVariables>{"".join(variables)}</ModelVariables>
  <ModelStructure>{structure}</ModelStructure>
</fmiModelDescription>
"""
    archive = tmp_path / f"{model}.fmu"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("modelDescription.xml", description)
        target.writestr(
            f"binaries/{platform_directory()}/{model}{library_suffix()}", b"")
    return archive


@pytest.fixture
def acc_fmus(tmp_path: Path) -> dict[str, Path]:
    return {
        "plant": acc_archive(tmp_path, "AccPlant", {
            "accel_mps2": ("m/s2", 0.0), "lead_accel_mps2": ("m/s2", 0.0),
            "initial_lead_position_m": ("m", 60.0),
        }, {
            "ego_position_m": "m", "ego_speed_mps": "m/s",
            "lead_position_m": "m", "lead_speed_mps": "m/s", "gap_m": "m",
            "relative_speed_mps": "m/s",
        }),
        "controller": acc_archive(tmp_path, "AccController", {
            "gap_m": ("m", 60.0), "relative_speed_mps": ("m/s", 0.0),
            "ego_speed_mps": ("m/s", 25.0),
        }, {"accel_mps2": "m/s2"}),
    }


@pytest.fixture
def no_binary_is_loaded(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("an FMU binary was loaded")
    monkeypatch.setattr(ctypes, "CDLL", refuse)


# The controller/plant composition ----------------------------------------------


@pytest.mark.usefixtures("no_binary_is_loaded")
class TestControllerPlant:
    def test_it_renders_the_manifest_the_closed_loop_proof_runs(
        self, tmp_path, acc_fmus, contract
    ):
        """Every participant and Channel of the #148 Run, and nothing else:
        the proof's in-run KPI participant is not an FMU of the coupling."""
        out = tmp_path / "acc.json"
        couple(EXAMPLE / "acc.json", acc_fmus, out)
        expected = contract.manifest("nominal").to_doc()
        del expected["participants"]["kpi"]
        for participant in expected["participants"].values():
            command = participant["command"]
            model = Path(command[3]).stem
            command[3] = str(acc_fmus[
                "plant" if model == "AccPlant" else "controller"].resolve())
        assert json.loads(out.read_text()) == expected

    def test_the_plan_states_one_period_of_delay_each_way(
        self, tmp_path, acc_fmus
    ):
        plan = couple(EXAMPLE / "acc.json", acc_fmus,
                      tmp_path / "acc.json")["plan"]
        assert plan["order"] == [
            {"fmu": "plant", "priority": 0, "step_period_ns": 10 * MS,
             "steps": 500},
            {"fmu": "controller", "priority": 1, "step_period_ns": 10 * MS,
             "steps": 500},
        ]
        assert plan["same_slot"] == []
        sensing = route(plan, "sensing", "controller")
        assert sensing["deliveries"][:2] == [
            {"published_ns": 0, "values_at_ns": 10 * MS,
             "delivered_ns": 10 * MS},
            {"published_ns": 10 * MS, "values_at_ns": 20 * MS,
             "delivered_ns": 20 * MS},
        ]
        # The plant runs first, so its Message of this Slot is queued behind
        # the one the controller drains: two in the route at once.
        assert sensing["peak_messages"] == 2
        assert route(plan, "command", "plant")["peak_messages"] == 1

    def test_a_latency_the_route_cannot_hold_is_refused_before_the_run(
        self, tmp_path, acc_fmus
    ):
        """The proof's overflow control, found while authoring: two Periods
        of Latency put three Messages in a route that holds two."""
        document = edited(ACC, lambda d: d["channels"]["sensing"].update(
            latency_ns=20 * MS))
        message = rejection(tmp_path, document, acc_fmus)
        assert "Channel 'sensing'" in message and "'controller'" in message
        assert "3 Messages" in message and "capacity 2" in message


def route(plan: dict, channel: str, subscriber: str) -> dict:
    (found,) = [r for r in plan["routes"]
                if (r["channel"], r["subscriber"]) == (channel, subscriber)]
    return found


# Declarations ------------------------------------------------------------------


class TestDeclarations:
    def test_reordered_declarations_render_the_same_bytes(self, tmp_path):
        def reorder(document):
            document["fmus"] = dict(reversed(document["fmus"].items()))
            document["channels"] = dict(reversed(document["channels"].items()))
            for fmu in document["fmus"].values():
                fmu["hold"].reverse()
        first, second = tmp_path / "first.json", tmp_path / "second.json"
        couple(EXAMPLE / "feedback.json", feedthroughs(tmp_path), first)
        couple(write(tmp_path, edited(FEEDBACK, reorder)),
               feedthroughs(tmp_path), second)
        assert first.read_bytes() == second.read_bytes()

    def test_a_renamed_fmu_keeps_its_place_in_the_slot(self, tmp_path):
        """A name no longer decides the order: `zulu` still runs first."""
        def rename(document):
            document["fmus"]["zulu"] = document["fmus"].pop("left")
            for channel in document["channels"].values():
                if channel["publisher"] == "left":
                    channel["publisher"] = "zulu"
                if "left" in channel["subscribers"]:
                    channel["subscribers"]["zulu"] = (
                        channel["subscribers"].pop("left"))
        document = edited(FEEDBACK, rename)
        receipt = couple(write(tmp_path, document),
                         feedthroughs(tmp_path, document),
                         tmp_path / "renamed.json")
        assert [f["fmu"] for f in receipt["plan"]["order"]] == [
            "zulu", "right"]
        manifest = json.loads((tmp_path / "renamed.json").read_text())
        assert manifest["participants"]["zulu"]["priority"] == 0

    def test_two_fmus_of_one_priority_are_refused(self, tmp_path):
        document = edited(FEEDBACK, lambda d: d["fmus"]["right"].update(
            priority=0))
        message = rejection(tmp_path, document)
        assert "'left' and 'right'" in message and "priority 0" in message

    @pytest.mark.parametrize("latency", [None, "missing"])
    def test_every_channel_states_its_latency(self, tmp_path, latency):
        def edit(document):
            channel = document["channels"]["left.value"]
            if latency == "missing":
                del channel["latency_ns"]
            else:
                channel["latency_ns"] = None
        message = rejection(tmp_path, edited(FEEDBACK, edit))
        assert "'left.value'" in message and "latency_ns" in message

    def test_an_fmu_without_an_archive_is_refused(self, tmp_path):
        with pytest.raises(AuthoringError, match="'right'.*no archive"):
            couple(EXAMPLE / "feedback.json", {"left": FEEDTHROUGH},
                   tmp_path / "manifest.json")

    def test_an_archive_for_no_declared_fmu_is_refused(self, tmp_path):
        fmus = {**feedthroughs(tmp_path), "extra": FEEDTHROUGH}
        with pytest.raises(AuthoringError, match="'extra'.*declares no FMU"):
            couple(EXAMPLE / "feedback.json", fmus, tmp_path / "m.json")

    def test_an_undeclared_publisher_is_refused(self, tmp_path):
        document = edited(FEEDBACK, lambda d: d["channels"]["left.value"]
                          .update(publisher="nobody"))
        assert "publisher 'nobody'" in rejection(tmp_path, document)

    def test_an_undeclared_subscriber_is_refused(self, tmp_path):
        def edit(document):
            subscribers = document["channels"]["left.value"]["subscribers"]
            subscribers["nobody"] = subscribers.pop("right")
        assert "subscriber 'nobody'" in rejection(tmp_path, edited(FEEDBACK, edit))


# Sources -----------------------------------------------------------------------


class TestSources:
    def test_an_input_nothing_feeds_is_refused(self, tmp_path):
        document = edited(FEEDBACK, lambda d: d["fmus"]["right"]["hold"]
                          .remove("Boolean_input"))
        message = rejection(tmp_path, document)
        assert "'right'" in message and "'Boolean_input'" in message
        assert "neither connected, started nor held" in message

    def test_an_input_fed_twice_is_refused(self, tmp_path):
        def edit(document):
            document["channels"]["extra"] = copy.deepcopy(
                document["channels"]["left.value"])
            document["channels"]["extra"]["subscribers"] = {
                "left": {"capacity": 2, "overflow": "fail",
                         "bind": {"value": "Float64_continuous_input"}}}
        message = rejection(tmp_path, edited(FEEDBACK, edit))
        assert "'left'" in message and "'Float64_continuous_input'" in message
        assert "fed by one connection" in message

    def test_a_connected_input_that_is_also_held_is_refused(self, tmp_path):
        document = edited(FEEDBACK, lambda d: d["fmus"]["right"]["hold"]
                          .append("Float64_continuous_input"))
        assert "also connected" in rejection(tmp_path, document)

    def test_a_field_no_subscriber_binds_is_refused(self, tmp_path):
        def edit(document):
            route = document["channels"]["left.value"]["subscribers"]["right"]
            route["bind"] = {"other": "Float64_continuous_input"}
        message = rejection(tmp_path, edited(FEEDBACK, edit))
        assert "field 'other'" in message and "'left.value'" in message

    def test_a_held_input_without_a_start_is_refused(self, tmp_path):
        """A held input keeps the start its FMU declares, so it declares one."""
        archive = tmp_path / "NoStart.fmu"
        with zipfile.ZipFile(FEEDTHROUGH) as source, \
                zipfile.ZipFile(archive, "w") as target:
            for member in source.infolist():
                data = source.read(member)
                if member.filename == "modelDescription.xml":
                    data = data.replace(
                        b'name="Boolean_input" valueReference="27" '
                        b'causality="input" start="false"',
                        b'name="Boolean_input" valueReference="27" '
                        b'causality="input"')
                target.writestr(member, data)
        message = rejection(tmp_path, FEEDBACK,
                            {"left": FEEDTHROUGH, "right": archive})
        assert "'right'" in message and "'Boolean_input'" in message
        assert "declares no start value" in message

    def test_a_connected_input_without_a_start_is_refused(self, tmp_path):
        """Until the first delivery the input holds its start, so one has to
        exist: declared by the FMU or given by the document."""
        archive = tmp_path / "NoStart.fmu"
        with zipfile.ZipFile(FEEDTHROUGH) as source, \
                zipfile.ZipFile(archive, "w") as target:
            for member in source.infolist():
                data = source.read(member)
                if member.filename == "modelDescription.xml":
                    data = data.replace(
                        b'causality="input" start="0" initial="exact"',
                        b'causality="input"')
                target.writestr(member, data)
        message = rejection(tmp_path, FEEDBACK,
                            {"left": FEEDTHROUGH, "right": archive})
        assert "'right'" in message and "no start value" in message


# Compatibility -----------------------------------------------------------------


class TestCompatibility:
    def test_an_output_connected_to_an_input_of_another_type_is_refused(
        self, tmp_path
    ):
        def edit(document):
            document["channels"]["left.value"]["subscribers"]["right"][
                "bind"] = {"value": "Boolean_input"}
            hold = document["fmus"]["right"]["hold"]
            hold.remove("Boolean_input")
            hold.append("Float64_continuous_input")
        message = rejection(tmp_path, edited(FEEDBACK, edit))
        assert "Float64" in message and "Boolean" in message

    def test_a_publisher_variable_that_is_an_input_is_refused(self, tmp_path):
        def edit(document):
            document["channels"]["left.value"]["fields"][0][
                "variable"] = "Float64_discrete_input"
            document["fmus"]["left"]["hold"].remove("Float64_discrete_input")
        message = rejection(tmp_path, edited(FEEDBACK, edit))
        assert "'Float64_discrete_input'" in message and "output" in message

    def test_a_unit_mismatch_needs_an_explicit_conversion(self, tmp_path):
        fmus = {
            "left": with_units(tmp_path, "Left",
                               {"Float64_continuous_output": "m"}),
            "right": with_units(tmp_path, "Right",
                                {"Float64_continuous_input": "km"}),
        }
        document = edited(FEEDBACK, lambda d: d["channels"]["left.value"][
            "fields"][0].update(unit="m"))
        message = rejection(tmp_path, document, fmus)
        assert "'m'" in message and "'km'" in message
        assert "converts no unit" in message

    def test_a_stated_unit_is_the_one_the_publisher_declares(self, tmp_path):
        fmus = {"left": with_units(tmp_path, "Left",
                                   {"Float64_continuous_output": "m"}),
                "right": FEEDTHROUGH}
        message = rejection(tmp_path, FEEDBACK, fmus)
        assert "states no unit" in message and "'m'" in message

    def test_the_importer_verdict_on_the_mapping_is_the_authoring_verdict(
        self, tmp_path
    ):
        """A field type the importer does not carry for the variable."""
        document = edited(FEEDBACK, lambda d: d["channels"]["left.value"][
            "fields"][0].update(type="i32"))
        assert "rejects the mapping" in rejection(tmp_path, document)


# Execution order -----------------------------------------------------------------


def zero_latency(document: dict, *channels: str) -> dict:
    def edit(d):
        for channel in channels:
            d["channels"][channel]["latency_ns"] = 0
    return edited(document, edit)


class TestExecutionOrder:
    def test_a_zero_latency_cycle_is_refused_by_name(self, tmp_path):
        message = rejection(
            tmp_path, zero_latency(FEEDBACK, "left.value", "right.value"))
        assert ("left -[left.value]-> right -[right.value]-> left"
                in message)
        assert "algebraic" not in message

    def test_a_delayed_feedback_loop_is_accepted(self, tmp_path):
        plan = couple(EXAMPLE / "feedback.json", feedthroughs(tmp_path),
                      tmp_path / "manifest.json")["plan"]
        assert plan["same_slot"] == []

    def test_one_zero_latency_direction_states_the_same_slot_order(
        self, tmp_path
    ):
        document = zero_latency(FEEDBACK, "left.value")
        plan = couple(write(tmp_path, document), feedthroughs(tmp_path),
                      tmp_path / "manifest.json")["plan"]
        assert plan["same_slot"] == [
            {"channel": "left.value", "publisher": "left",
             "subscriber": "right"}]
        assert route(plan, "left.value", "right")["deliveries"][0] == {
            "published_ns": 0, "values_at_ns": 10 * MS, "delivered_ns": 0}

    def test_a_priority_against_a_zero_latency_connection_is_refused(
        self, tmp_path
    ):
        """Reversing the order is an authored change, and the check names the
        order the connection needs."""
        document = zero_latency(FEEDBACK, "left.value")
        document["fmus"]["left"]["priority"] = 2
        message = rejection(tmp_path, document)
        assert "'left' (priority 2)" in message
        assert "'right' (priority 1)" in message
        assert "left, right" in message


# The readable plan ---------------------------------------------------------------


def test_the_plan_reads_as_periods_order_and_delivery_points(tmp_path):
    receipt = couple(EXAMPLE / "feedback.json", feedthroughs(tmp_path),
                     tmp_path / "manifest.json")
    text = render_plan(receipt["plan"])
    assert "1. left  priority 0, period 10 ms" in text
    assert "2. right  priority 1, period 10 ms" in text
    assert ("left.value, published by left with Latency 10 ms: "
            "value = Float64_continuous_output") in text
    assert ("    to right: route capacity 2 (fail), at most 2 Messages "
            "in the route") in text
    assert "    to left: route capacity 1 (fail), at most 1 Message in the route" in text
    assert ("right.Float64_continuous_input holds 0 (FMU start) until the "
            "first delivery") in text
    assert "left.Float64_continuous_input holds 1.5 (document start)" in text
    assert ("at 10 ms: takes the Message published at 0 ms (values at 10 ms)"
            in text)
    assert "same-Slot connections: none" in text


def test_a_route_that_drops_lists_what_it_delivers(tmp_path):
    """Under drop_newest the route refuses a Message that finds it full, so
    the plan lists the deliveries the kernel makes and the drops."""
    document = edited(FEEDBACK, lambda d: d["channels"]["left.value"][
        "subscribers"]["right"].update(capacity=1, overflow="drop_newest"))
    receipt = couple(write(tmp_path, document), feedthroughs(tmp_path),
                     tmp_path / "manifest.json")
    dropping = route(receipt["plan"], "left.value", "right")
    # `left` publishes at 10 ms before `right` drains the Message of 0 ms,
    # so the route is full and every second Message is refused.
    assert dropping["deliveries"] == [
        {"published_ns": 0, "values_at_ns": 10 * MS, "delivered_ns": 10 * MS},
        {"published_ns": 20 * MS, "values_at_ns": 30 * MS,
         "delivered_ns": 30 * MS},
    ]
    assert dropping["first_dropped_ns"] == [10 * MS, 30 * MS]
    assert dropping["dropped_messages"] == 2
    assert "2 Messages dropped, the first published at 10 ms, 30 ms" in (
        render_plan(receipt["plan"]))


class TestCommand:
    def test_it_writes_the_manifest_and_the_receipt(self, tmp_path, capsys):
        out, receipt = tmp_path / "m.json", tmp_path / "receipt.json"
        assert main([str(EXAMPLE / "feedback.json"),
                     "--fmu", "left", str(FEEDTHROUGH),
                     "--fmu", "right", str(FEEDTHROUGH),
                     "-o", str(out), "--receipt", str(receipt)]) == 0
        written = json.loads(receipt.read_text())
        assert written["sil_fmu_coupling_receipt"] == 1
        assert set(written["fmus"]) == {"left", "right"}
        assert "execution order" in capsys.readouterr().out

    def test_it_refuses_with_exit_2_and_writes_nothing(self, tmp_path, capsys):
        document = write(tmp_path, zero_latency(
            FEEDBACK, "left.value", "right.value"))
        out = tmp_path / "m.json"
        assert main([str(document), "--fmu", "left", str(FEEDTHROUGH),
                     "--fmu", "right", str(FEEDTHROUGH), "-o", str(out)]) == 2
        assert "cycle" in capsys.readouterr().err
        assert not out.exists()


# End to end ----------------------------------------------------------------------


def values(recording: Path, channel: str) -> list[tuple[int, float]]:
    return [(t, struct.unpack("<d", data)[0])
            for topic, t, data in read_records(recording) if topic == channel]


class TestRun:
    def test_a_delayed_feedback_loop_runs_as_its_plan_states(
        self, tmp_path, sil_run
    ):
        """`left` starts from 1.5 and `right` from 0. Each value crosses in
        one Period, so the two alternate, and both Runs are one Run."""
        manifest = tmp_path / "feedback.json"
        couple(EXAMPLE / "feedback.json", feedthroughs(tmp_path), manifest)
        first = run_manifest(sil_run, manifest, tmp_path / "run-1.mcap")
        second = run_manifest(sil_run, manifest, tmp_path / "run-2.mcap")
        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()
        times = [0, 10 * MS, 20 * MS, 30 * MS, 40 * MS]
        assert values(first.mcap_path, "left.value") == list(
            zip(times, [1.5, 0.0, 1.5, 0.0, 1.5]))
        assert values(first.mcap_path, "right.value") == list(
            zip(times, [0.0, 1.5, 0.0, 1.5, 0.0]))

    def test_a_zero_latency_connection_delivers_in_the_same_slot(
        self, tmp_path, sil_run
    ):
        manifest = tmp_path / "same-slot.json"
        couple(write(tmp_path, zero_latency(FEEDBACK, "left.value")),
               feedthroughs(tmp_path), manifest)
        proc = run_manifest(sil_run, manifest, tmp_path / "run.mcap")
        assert proc.returncode == 0, proc.stderr
        assert values(proc.mcap_path, "right.value")[0] == (0, 1.5)

    def test_a_dropping_route_delivers_what_its_plan_lists(
        self, tmp_path, sil_run
    ):
        """The plan drops `left`'s 0 of 10 ms, so `right` takes nothing at
        20 ms and keeps the 1.5 it took at 10 ms. Delivered, the 0 would
        have reached `right` at 20 ms."""
        document = edited(FEEDBACK, lambda d: d["channels"]["left.value"][
            "subscribers"]["right"].update(capacity=1, overflow="drop_newest"))
        manifest = tmp_path / "dropping.json"
        couple(write(tmp_path, document), feedthroughs(tmp_path), manifest)
        proc = run_manifest(sil_run, manifest, tmp_path / "run.mcap")
        assert proc.returncode == 0, proc.stderr
        assert [v for _, v in values(proc.mcap_path, "left.value")][:3] == [
            1.5, 0.0, 1.5]
        assert [v for _, v in values(proc.mcap_path, "right.value")] == [
            0.0, 1.5, 1.5, 1.5, 1.5]


# Several Periods (#195) ----------------------------------------------------------
#
# `ball` (BouncingBall, 20 ms) publishes its height. `fast` (10 ms) and `slow`
# (30 ms) are `Feedthrough` instances that declare the height's unit: each one
# publishes at `a` the input it stepped on at `a`. All three share the Slots
# 0, 60 ms and 120 ms, and the Duration is a multiple of every Period.

FEEDTHROUGH_INPUTS = [
    "Float32_continuous_input", "Float32_discrete_input",
    "Float64_continuous_input", "Float64_discrete_input", "Int8_input",
    "UInt8_input", "Int16_input", "UInt16_input", "Int32_input",
    "UInt32_input", "Int64_input", "UInt64_input", "Boolean_input",
    "String_input", "Binary_input", "Enumeration_input",
]
BOUNCING_BALL = FEEDTHROUGH.parent / "BouncingBall.fmu"
HEIGHT = {"name": "h", "type": "f64", "unit": "m"}


def probe(period_ms: int, priority: int, start: str) -> dict:
    return {
        "step_period_ns": period_ms * MS, "priority": priority,
        "start": [{"variable": "Float64_continuous_input", "value": start,
                   "unit": "m"}],
        "hold": [v for v in FEEDTHROUGH_INPUTS
                 if v != "Float64_continuous_input"],
    }


def feeds(publisher: str, variable: str, subscriber: str,
          capacity: int) -> dict:
    return {
        "publisher": publisher, "latency_ns": 10 * MS,
        "fields": [{**HEIGHT, "variable": variable}],
        "subscribers": {subscriber: {
            "capacity": capacity, "overflow": "fail",
            "bind": {"h": "Float64_continuous_input"}}},
    }


MULTIRATE = {
    "sil_fmu_coupling": 1,
    "duration_ns": 120 * MS,
    "fmus": {
        "ball": {"step_period_ns": 20 * MS, "priority": 0, "start": [],
                 "hold": []},
        "fast": probe(10, 1, "1.25"),
        "slow": probe(30, 2, "2.5"),
    },
    "channels": {
        "ball": feeds("ball", "h", "fast", 1),
        "fast": feeds("fast", "Float64_continuous_output", "slow", 4),
        "slow": {"publisher": "slow", "latency_ns": 10 * MS,
                 "fields": [{**HEIGHT, "variable": "Float64_continuous_output"}],
                 "subscribers": {}},
    },
}

# Which input each activation steps on, stated by hand from the Periods and
# the Latencies: "start", or the publication Slot of the Message it holds.
# `fast` takes a new height every second activation and holds it in between.
FAST_TAKES = {0: "start", 10: 0, 20: 0, 30: 20, 40: 20, 50: 40, 60: 40,
              70: 60, 80: 60, 90: 80, 100: 80, 110: 100}
# `slow` takes three Messages of `fast` at once and steps on the newest.
SLOW_TAKES = {0: "start", 30: 20, 60: 50, 90: 80}


@pytest.fixture
def multirate_fmus(tmp_path: Path) -> dict[str, Path]:
    tagged = {"Float64_continuous_input": "m", "Float64_continuous_output": "m"}
    return {"ball": BOUNCING_BALL,
            "fast": with_units(tmp_path, "Fast", tagged),
            "slow": with_units(tmp_path, "Slow", tagged)}


def taken(published_ms: int, period_ms: int) -> dict:
    return {"published_ns": published_ms * MS,
            "values_at_ns": (published_ms + period_ms) * MS}


class TestSeveralPeriods:
    def plan(self, tmp_path, fmus, document=MULTIRATE) -> dict:
        return couple(write(tmp_path, document), fmus,
                      tmp_path / "multirate.json")["plan"]

    def test_each_activation_states_the_input_it_steps_on(
        self, tmp_path, multirate_fmus
    ):
        """The first activation holds the start value, an activation with no
        new Message holds the last one, and an activation that takes several
        steps on the newest."""
        plan = self.plan(tmp_path, multirate_fmus)
        assert route(plan, "ball", "fast")["activations"] == [
            {"at_ns": 0, "delivered": 0, "input": "start"},
            {"at_ns": 10 * MS, "delivered": 1, "input": taken(0, 20)},
            {"at_ns": 20 * MS, "delivered": 0, "input": taken(0, 20)},
        ]
        assert route(plan, "fast", "slow")["activations"] == [
            {"at_ns": 0, "delivered": 0, "input": "start"},
            {"at_ns": 30 * MS, "delivered": 3, "input": taken(20, 10)},
        ]

    def test_in_a_shared_slot_a_zero_latency_input_is_the_new_sample(
        self, tmp_path, multirate_fmus
    ):
        """`ball` runs before `fast` in the Slots they share, so with Latency
        0 `fast` steps on the height published in the same Slot, and holds it
        in the Slot `ball` does not have."""
        document = edited(MULTIRATE, lambda d: d["channels"]["ball"].update(
            latency_ns=0))
        assert route(self.plan(tmp_path, multirate_fmus, document), "ball",
                     "fast")["activations"] == [
            {"at_ns": 0, "delivered": 1, "input": taken(0, 20)},
            {"at_ns": 10 * MS, "delivered": 0, "input": taken(0, 20)},
        ]

    def test_a_plan_that_stops_before_the_pattern_repeats_says_so(
        self, tmp_path
    ):
        """`right` has 13 activations before the pattern of a 130 ms
        publisher repeats; the plan lists 12 and states the one it omits."""
        def edit(document):
            document["duration_ns"] = 130 * MS
            document["fmus"]["left"]["step_period_ns"] = 130 * MS
            document["channels"]["right.value"]["subscribers"]["left"][
                "capacity"] = 13
        receipt = couple(write(tmp_path, edited(FEEDBACK, edit)),
                         feedthroughs(tmp_path), tmp_path / "manifest.json")
        slow_to_fast = route(receipt["plan"], "left.value", "right")
        assert [a["at_ns"] for a in slow_to_fast["activations"]] == [
            t * MS for t in range(0, 120, 10)]
        assert slow_to_fast["activations_not_listed"] == 1
        assert route(receipt["plan"], "right.value", "left")[
            "activations_not_listed"] == 0
        assert ("1 more activation before the pattern repeats is not listed"
                in render_plan(receipt["plan"]))

    def test_the_start_values_state_where_they_come_from(
        self, tmp_path, multirate_fmus
    ):
        plan = self.plan(tmp_path, multirate_fmus)
        assert route(plan, "ball", "fast")["until_first_delivery"] == {
            "Float64_continuous_input": {"value": "1.25", "from": "document"}}
        assert route(plan, "fast", "slow")["until_first_delivery"] == {
            "Float64_continuous_input": {"value": "2.5", "from": "document"}}

    def test_a_start_the_fmu_declares_is_named_as_the_fmus(self, tmp_path):
        plan = couple(EXAMPLE / "feedback.json", feedthroughs(tmp_path),
                      tmp_path / "manifest.json")["plan"]
        assert route(plan, "left.value", "right")["until_first_delivery"] == {
            "Float64_continuous_input": {"value": "0", "from": "FMU"}}

    def test_the_plan_states_the_periods_and_latencies(
        self, tmp_path, multirate_fmus
    ):
        text = render_plan(self.plan(tmp_path, multirate_fmus))
        assert "Duration 120 ms: Slots at 0 <= t < 120 ms" in text
        assert "1. ball  priority 0, period 20 ms, 6 Steps" in text
        assert "3. slow  priority 2, period 30 ms, 4 Steps" in text
        assert "at 0 ms: no Message yet, holds the start value" in text
        assert ("at 20 ms: no new Message, holds the one published at 0 ms "
                "(values at 20 ms)") in text
        assert ("at 30 ms: takes 3 Messages and steps on the newest, "
                "published at 20 ms (values at 30 ms)") in text
        assert ("fast.Float64_continuous_input holds 1.25 (document start) "
                "until the first delivery") in text

    def test_the_final_publication_of_each_channel(
        self, tmp_path, multirate_fmus
    ):
        """Each FMU's last Step ends on the Duration. `fast` still takes the
        last height in-run; the last Messages of `fast` and `slow` become
        visible at or after the Duration, so only the Recording holds them."""
        plan = self.plan(tmp_path, multirate_fmus)
        finals = {c["channel"]: c["final"] for c in plan["channels"]}
        assert finals == {
            "ball": {"published_ns": 100 * MS, "values_at_ns": 120 * MS,
                     "taken_by": {"fast": 110 * MS}},
            "fast": {"published_ns": 110 * MS, "values_at_ns": 120 * MS,
                     "taken_by": {}},
            "slow": {"published_ns": 90 * MS, "values_at_ns": 120 * MS,
                     "taken_by": {}},
        }
        text = render_plan(plan)
        assert ("last Message published at 110 ms (values at 120 ms): no "
                "activation takes it; only the Recording holds it") in text
        assert ("last Message published at 100 ms (values at 120 ms): fast "
                "takes it at 110 ms") in text

    @pytest.mark.parametrize("duration_ms", [100, 125])
    def test_a_duration_that_splits_a_step_is_refused(
        self, tmp_path, multirate_fmus, duration_ms
    ):
        document = edited(MULTIRATE, lambda d: d.update(
            duration_ns=duration_ms * MS))
        message = rejection(tmp_path, document, multirate_fmus)
        assert f"Duration {duration_ms} ms" in message
        assert "'slow'" in message and "period 30 ms" in message
        assert "does not clip" in message
        assert "120 ms" in message

    def test_a_duration_that_splits_a_step_of_the_feedback_loop_is_refused(
        self, tmp_path
    ):
        document = edited(FEEDBACK, lambda d: d.update(duration_ns=55 * MS))
        message = rejection(tmp_path, document)
        assert "Duration 55 ms" in message and "'left'" in message
        assert "50 ms or 60 ms" in message


def expected_heights(recording: Path) -> dict[str, dict[int, float]]:
    """What `fast` and `slow` must publish, by sample time, from the heights
    `ball` published and the tables above. Nothing here reads the plan."""
    heights = dict(values(recording, "ball"))
    fast = {published * MS: 1.25 if source == "start" else heights[source * MS]
            for published, source in FAST_TAKES.items()}
    slow = {published * MS: 2.5 if source == "start" else fast[source * MS]
            for published, source in SLOW_TAKES.items()}
    return {"fast": {t + 10 * MS: v for t, v in fast.items()},
            "slow": {t + 30 * MS: v for t, v in slow.items()}}


def final_coverage_contract() -> dict:
    """Each probe's output, observed at the time it describes: the end of
    the Step, one Period after its publication, up to the Duration."""
    def observed(period_ms: int) -> dict:
        return {"actual_offset_ns": period_ms * MS, "reference_offset_ns": 0,
                "observations": {"start_ns": period_ms * MS,
                                 "stop_ns": 120 * MS,
                                 "step_ns": period_ms * MS},
                # Nothing is computed between the two sides: equal bits.
                "fields": {"h": {"atol": 0, "rtol": 0}}}
    return {"sil_comparison": 1,
            "evaluation": {"from_ns": 0, "to_ns": 120 * MS},
            "channels": {"fast": observed(10), "slow": observed(30)}}


def write_reference(path: Path, expected: dict[str, dict[int, float]]) -> Path:
    from mcap.writer import CompressionType, Writer
    schema = json.dumps({"fields": [{"name": "h", "type": "f64"}]},
                        sort_keys=True).encode()
    with open(path, "wb") as f:
        writer = Writer(f, compression=CompressionType.NONE)
        writer.start(profile="sil", library="test")
        for channel, samples in expected.items():
            schema_id = writer.register_schema(channel, "sil_pod", schema)
            channel_id = writer.register_channel(channel, "sil_pod", schema_id)
            for t, value in sorted(samples.items()):
                writer.add_message(channel_id, log_time=t, publish_time=t,
                                   data=struct.pack("<d", value))
        writer.finish()
    return path


class TestSeveralPeriodsRun:
    @pytest.fixture
    def recording(self, tmp_path, multirate_fmus, sil_run) -> Path:
        manifest = tmp_path / "multirate.json"
        couple(write(tmp_path, MULTIRATE, "document.json"), multirate_fmus,
               manifest)
        first = run_manifest(sil_run, manifest, tmp_path / "run-1.mcap")
        second = run_manifest(sil_run, manifest, tmp_path / "run-2.mcap")
        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert first.mcap_path.read_bytes() == second.mcap_path.read_bytes()
        return first.mcap_path

    def test_each_fmu_publishes_one_message_per_step_up_to_the_duration(
        self, recording
    ):
        for channel, period_ms in (("ball", 20), ("fast", 10), ("slow", 30)):
            assert [t for t, _ in values(recording, channel)] == [
                t * MS for t in range(0, 120, period_ms)]

    def test_each_activation_steps_on_the_input_the_tables_state(
        self, recording
    ):
        expected = expected_heights(recording)
        for channel, period_ms in (("fast", 10), ("slow", 30)):
            assert {t + period_ms * MS: v
                    for t, v in values(recording, channel)} == expected[channel]
        # The heights differ, so a wrong table could not pass by chance.
        assert len(set(expected["fast"].values())) == 7

    def test_a_post_hoc_check_covers_the_final_sample(
        self, tmp_path, recording
    ):
        from sil.compare import compare, read_contract
        contract_path = tmp_path / "contract.json"
        contract_path.write_text(json.dumps(final_coverage_contract()))
        contract = read_contract(contract_path)
        expected = expected_heights(recording)
        good = write_reference(tmp_path / "good.mcap", expected)
        report = compare(contract, recording, good)
        assert report["verdict"] == "pass", report
        assert {name: counts["checked"]
                for name, counts in report["channels"].items()} == {
            "fast": 12, "slow": 4}

        wrong = copy.deepcopy(expected)
        wrong["slow"][120 * MS] += 0.5
        report = compare(contract, recording,
                         write_reference(tmp_path / "wrong.mcap", wrong))
        assert report["verdict"] == "fail"
        first = report["first_divergence"]
        assert (first["kind"], first["channel"], first["observation_ns"],
                first["actual_publication_ns"]) == (
            "value", "slow", 120 * MS, 90 * MS)
