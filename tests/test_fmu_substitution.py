"""`sil-fmu-substitute`: replace one coupled FMU with its Recording (#198).

A coupled Run is recorded, one FMU is removed, and a Replay participant
publishes the Channels it fed the retained FMUs. The retained outputs of the
replacement Run are compared with the original Run's at every Sample time.
The Runs step the reference FMUs: `Feedthrough` in a delayed feedback loop
of one Period, and `BouncingBall` feeding two `Feedthrough` instances at
three Periods.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from conftest import run_manifest
from test_fmu_coupling import (BOUNCING_BALL, EXAMPLE, FEEDBACK, MS, MULTIRATE,
                               feedthroughs, values, with_units, write,
                               zero_latency)

from sil.compare import compare, read_contract
from sil.fmi.coupling import couple
from sil.fmi.substitution import AuthoringError, main, substitute
from sil.recording import read_records


def recorded(tmp_path: Path, sil_run, document: Path, fmus: dict) -> Path:
    """The Recording of the original, live Run."""
    manifest = tmp_path / "original.json"
    couple(document, fmus, manifest)
    proc = run_manifest(sil_run, manifest, tmp_path / "original.mcap")
    assert proc.returncode == 0, proc.stderr
    return proc.mcap_path


class Replaced:
    """One replacement authored and run twice."""

    def __init__(self, tmp_path: Path, sil_run, document: Path, fmus: dict,
                 replaced: str):
        self.original = recorded(tmp_path, sil_run, document, fmus)
        self.manifest = tmp_path / "replacement.json"
        self.contract = tmp_path / "contract.json"
        self.receipt = substitute(document, fmus, replaced, self.original,
                                  self.manifest, self.contract)
        self.sil_run = sil_run
        self.tmp_path = tmp_path
        runs = [self.run(self.manifest, f"replacement-{n}") for n in (1, 2)]
        self.recording = runs[0]
        self.repeated = runs[1]

    def run(self, manifest: Path, name: str) -> Path:
        proc = run_manifest(self.sil_run, manifest,
                            self.tmp_path / f"{name}.mcap")
        assert proc.returncode == 0, proc.stderr
        return proc.mcap_path

    def compared(self, recording: Path) -> dict:
        return self.compared_with(recording, self.original)

    def compared_with(self, recording: Path, reference: Path) -> dict:
        return compare(read_contract(self.contract), recording, reference)

    def variant(self, name: str, edit) -> Path:
        """The replacement Manifest with one defect, as a hand edit makes it."""
        doc = json.loads(self.manifest.read_text())
        edit(doc)
        path = self.tmp_path / f"{name}.json"
        path.write_text(json.dumps(doc))
        return self.run(path, name)


@pytest.fixture
def feedback(tmp_path, sil_run) -> Replaced:
    """`right` is retained; `left` is replaced by its `left.value`."""
    return Replaced(tmp_path, sil_run, EXAMPLE / "feedback.json",
                    feedthroughs(tmp_path), "left")


class TestEqualPeriods:
    def test_the_retained_outputs_are_the_original_ones(self, feedback):
        report = feedback.compared(feedback.recording)
        assert report["verdict"] == "pass", report["first_divergence"]
        # Every Sample time from the first Step's end through the Duration.
        assert {name: c["checked"] for name, c in report["channels"].items()
                } == {"left.value": 5, "right.value": 5}
        assert values(feedback.recording, "right.value") == values(
            feedback.original, "right.value")

    def test_the_replay_is_the_one_publisher_of_the_boundary(self, feedback):
        replacement = json.loads(feedback.manifest.read_text())
        replay = replacement["participants"]["left"]
        assert replay["type"] == "replay"
        assert replay["channels"] == ["left.value"]
        assert replay["recording_hash"] == \
            feedback.receipt["recording"]["sha256"]
        assert [name for name, p in replacement["participants"].items()
                if "left.value" in p.get("publishes", [])] == []

    def test_the_retained_fmu_and_the_channels_are_declared_as_before(
        self, feedback
    ):
        """Command, start values, bindings, Period, priority, routes;
        schema and Latency of each Channel."""
        original = json.loads((feedback.tmp_path / "original.json").read_text())
        replacement = json.loads(feedback.manifest.read_text())
        assert replacement["participants"]["right"] == \
            original["participants"]["right"]
        assert replacement["channels"] == original["channels"]
        assert replacement["schemas"] == original["schemas"]
        assert replacement["duration_ns"] == original["duration_ns"]

    def test_two_runs_of_the_replacement_are_one_recording(self, feedback):
        """Bytes are compared only for one Manifest: the original Run's
        Recording names another Manifest and differs in bytes."""
        assert feedback.recording.read_bytes() == \
            feedback.repeated.read_bytes()
        assert feedback.recording.read_bytes() != \
            feedback.original.read_bytes()

    def test_the_receipt_states_the_warm_up_and_the_final_sample(
        self, feedback
    ):
        """`right` holds its start value until it first takes `left`'s
        Message at 10 ms; the first compared Sample time is the end of its
        first Step, and the last is the Duration."""
        receipt = feedback.receipt
        assert receipt["boundary"] == [{
            "channel": "left.value", "latency_ns": 10 * MS,
            "same_slot": False, "fields": ["value"], "publications": 5,
            "first_delivery_ns": {"right": 10 * MS}}]
        assert receipt["compared"]["right.value"] == {
            "publisher": "right", "offset_ns": 10 * MS, "first_ns": 10 * MS,
            "last_ns": 50 * MS, "count": 5}
        assert receipt["recording"]["manifest_sha256"] == \
            couple_hash(feedback.tmp_path / "original.json")
        assert "unchanged retained experiment" in receipt["validity"]


def couple_hash(manifest: Path) -> str:
    import hashlib
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def later(channel: str, latency_ns: int, subscriber: str, capacity: int):
    """A Latency other than the recorded Run's. The route of `subscriber`
    gets room for the Messages the longer Latency keeps in it, so the Run
    completes and the comparison judges it."""
    def edit(doc):
        doc["channels"][channel]["latency_ns"] = latency_ns
        for route in doc["participants"][subscriber]["subscribes"]:
            if route["channel"] == channel:
                route["capacity"] = capacity
    return edit


def without_first(channel: str):
    """Drop what the Replay participant publishes at 0 on `channel`."""
    def edit(doc):
        doc["channels"][channel]["interceptors"] = [
            {"kind": "drop", "start_ns": 0, "end_ns": 1}]
    return edit


def first_divergence(report: dict) -> tuple:
    first = report["first_divergence"]
    return (first["kind"], first["channel"], first["observation_ns"],
            first["actual"], first["expected"])


class TestEqualPeriodControls:
    def test_a_wrong_latency_diverges_where_right_takes_its_first_input(
        self, feedback
    ):
        """At 20 ms Latency `right` takes nothing at 10 ms and publishes
        its start value 0 where the original took `left`'s 1.5."""
        report = feedback.compared(
            feedback.variant("wrong-latency", later("left.value", 20 * MS, "right", 3)))
        assert report["verdict"] == "fail"
        assert first_divergence(report) == (
            "value", "right.value", 20 * MS, 0.0, 1.5)

    def test_an_omitted_initial_input_is_the_first_divergence(self, feedback):
        """Without `left`'s Message of 0 ms, the boundary misses its first
        Sample time, and `right` then holds its start value."""
        report = feedback.compared(
            feedback.variant("omitted", without_first("left.value")))
        assert report["verdict"] == "fail"
        assert first_divergence(report) == (
            "missing-actual", "left.value", 10 * MS, None, {"value": 1.5})
        assert report["channels"]["right.value"]["failed"] == 1

    def test_a_changed_retained_experiment_is_refused(self, tmp_path, feedback):
        changed = copy.deepcopy(FEEDBACK)
        changed["fmus"]["right"]["start"] = [{
            "variable": "Float64_continuous_input", "value": "0.75",
            "unit": None}]
        with pytest.raises(AuthoringError) as raised:
            substitute(write(tmp_path, changed, "changed.json"),
                       feedthroughs(tmp_path), "left", feedback.original,
                       tmp_path / "changed-replacement.json",
                       tmp_path / "changed-contract.json")
        assert "unchanged retained experiment" in str(raised.value)
        assert "live Run" in str(raised.value)
        assert not (tmp_path / "changed-replacement.json").exists()
        assert not (tmp_path / "changed-contract.json").exists()

    def test_recorded_feedback_does_not_respond_to_a_changed_experiment(
        self, tmp_path, sil_run, feedback
    ):
        """Why the change is refused. With `right` started at 0.75, the live
        `left` echoes 0.75 back at 10 ms; the recorded one still holds the
        original 0, so the loop is another one from there on."""
        changed = copy.deepcopy(FEEDBACK)
        changed["fmus"]["right"]["start"] = [{
            "variable": "Float64_continuous_input", "value": "0.75",
            "unit": None}]
        live = tmp_path / "live-changed"
        live.mkdir()
        live_recording = recorded(live, sil_run,
                                  write(live, changed, "changed.json"),
                                  feedthroughs(tmp_path))

        def start(doc):
            doc["participants"]["right"]["command"] += [
                "--start", "Float64_continuous_input=0.75"]
        replayed = feedback.variant("replayed-changed", start)
        report = feedback.compared_with(replayed, live_recording)
        assert report["verdict"] == "fail"
        assert first_divergence(report) == (
            "value", "left.value", 20 * MS, 0.0, 0.75)


def test_a_zero_latency_boundary_delivers_inside_the_slot_as_before(
    tmp_path, sil_run
):
    """The Replay participant publishes before any activation of a Slot, so
    `right` takes `left`'s Message in its publishing Slot, as it did live."""
    replaced = Replaced(
        tmp_path, sil_run,
        write(tmp_path, zero_latency(FEEDBACK, "left.value"), "same-slot.json"),
        feedthroughs(tmp_path), "left")
    [boundary] = replaced.receipt["boundary"]
    assert (boundary["same_slot"], boundary["first_delivery_ns"]) == (
        True, {"right": 0})
    assert values(replaced.recording, "right.value")[0] == (0, 1.5)
    report = replaced.compared(replaced.recording)
    assert report["verdict"] == "pass", report["first_divergence"]


@pytest.fixture
def multirate(tmp_path, sil_run) -> Replaced:
    """`ball` (20 ms) is replaced by its heights; `fast` (10 ms) and
    `slow` (30 ms) are retained, and `slow` takes only `fast`."""
    tagged = {"Float64_continuous_input": "m", "Float64_continuous_output": "m"}
    fmus = {"ball": BOUNCING_BALL,
            "fast": with_units(tmp_path, "Fast", tagged),
            "slow": with_units(tmp_path, "Slow", tagged)}
    return Replaced(tmp_path, sil_run,
                    write(tmp_path, MULTIRATE, "multirate.json"), fmus, "ball")


class TestSeveralPeriods:
    def test_the_retained_outputs_are_the_original_ones(self, multirate):
        report = multirate.compared(multirate.recording)
        assert report["verdict"] == "pass", report["first_divergence"]
        # Through the final sample at 120 ms, where only the Recording
        # holds the last Messages of `fast` and `slow`.
        assert {name: c["checked"] for name, c in report["channels"].items()
                } == {"ball": 6, "fast": 12, "slow": 4}
        assert multirate.recording.read_bytes() == \
            multirate.repeated.read_bytes()

    def test_the_receipt_states_the_boundary_and_the_samples(self, multirate):
        receipt = multirate.receipt
        assert receipt["retained"] == ["fast", "slow"]
        assert [(b["channel"], b["publications"], b["first_delivery_ns"])
                for b in receipt["boundary"]] == [
            ("ball", 6, {"fast": 10 * MS})]
        assert {name: (c["first_ns"], c["last_ns"], c["count"])
                for name, c in receipt["compared"].items()} == {
            "ball": (20 * MS, 120 * MS, 6), "fast": (10 * MS, 120 * MS, 12),
            "slow": (30 * MS, 120 * MS, 4)}

    def test_a_wrong_latency_diverges_where_fast_takes_its_first_height(
        self, multirate
    ):
        """At 20 ms Latency `fast` takes nothing at 10 ms and publishes its
        start value 1.25 where the original took the height of 0 ms."""
        report = multirate.compared(
            multirate.variant("wrong-latency", later("ball", 20 * MS, "fast", 2)))
        assert report["verdict"] == "fail"
        kind, channel, at, actual, expected = first_divergence(report)
        assert (kind, channel, at, actual) == ("value", "fast", 20 * MS, 1.25)
        assert expected == values(multirate.original, "ball")[0][1]

    def test_an_omitted_initial_input_is_the_first_divergence(self, multirate):
        report = multirate.compared(
            multirate.variant("omitted", without_first("ball")))
        assert report["verdict"] == "fail"
        kind, channel, at, actual, _ = first_divergence(report)
        assert (kind, channel, at, actual) == (
            "missing-actual", "ball", 20 * MS, None)
        # `fast` steps on the height of 0 ms at 10 and 20 ms and holds its
        # start value at both; `slow` steps on the second at 30 ms.
        assert report["channels"]["fast"]["failed"] == 2
        assert report["channels"]["slow"]["failed"] == 1


# Refusals ----------------------------------------------------------------------


def rewritten(original: Path, path: Path, keep, *, run: bool = True) -> Path:
    """`original` without the Messages `keep` refuses, and without its
    Manifest hash when it is not a Run's."""
    from mcap.reader import make_reader
    from mcap.writer import CompressionType, Writer
    with open(original, "rb") as f:
        reader = make_reader(f)
        summary = reader.get_summary()
        metadata = list(reader.iter_metadata())
        messages = list(reader.iter_messages())
    with open(path, "wb") as f:
        writer = Writer(f, compression=CompressionType.NONE)
        writer.start(profile="sil", library="test")
        if run:
            for m in metadata:
                writer.add_metadata(m.name, m.metadata)
        ids = {}
        for channel in summary.channels.values():
            schema = summary.schemas[channel.schema_id]
            schema_id = writer.register_schema(schema.name, schema.encoding,
                                               schema.data)
            ids[channel.id] = writer.register_channel(
                channel.topic, channel.message_encoding, schema_id)
        for _, channel, message in messages:
            if keep(channel.topic, message.log_time):
                writer.add_message(ids[channel.id], log_time=message.log_time,
                                   publish_time=message.publish_time,
                                   data=message.data)
        writer.finish()
    return path


def refusal(feedback: Replaced, replaced: str = "left",
            recording: Path | None = None, document: Path | None = None,
            fmus: dict | None = None) -> str:
    out = feedback.tmp_path / "refused.json"
    contract = feedback.tmp_path / "refused-contract.json"
    with pytest.raises(AuthoringError) as raised:
        substitute(document or EXAMPLE / "feedback.json",
                   fmus or feedthroughs(feedback.tmp_path), replaced,
                   recording or feedback.original, out, contract)
    assert not out.exists() and not contract.exists()
    return str(raised.value)


class TestRefusals:
    def test_an_fmu_the_document_does_not_declare(self, feedback):
        assert "'plant', which the document does not declare" in refusal(
            feedback, "plant")

    def test_an_fmu_that_feeds_no_other_fmu(self, tmp_path, feedback):
        document = copy.deepcopy(FEEDBACK)
        del document["channels"]["left.value"]["subscribers"]["right"]
        document["fmus"]["right"]["start"] = [{
            "variable": "Float64_continuous_input", "value": "0",
            "unit": None}]
        # A Recording of that Run is not needed to refuse it.
        assert "'left' feeds no other FMU" in refusal(
            feedback, document=write(tmp_path, document, "open.json"))

    def test_a_recording_no_run_wrote(self, tmp_path, feedback):
        converted = rewritten(feedback.original, tmp_path / "converted.mcap",
                              lambda topic, t: True, run=False)
        assert "names no Manifest" in refusal(feedback, recording=converted)

    def test_an_incomplete_recording(self, tmp_path, feedback):
        """A Run that failed at 30 ms: the Manifest is the same one."""
        failed = rewritten(feedback.original, tmp_path / "failed.mcap",
                           lambda topic, t: t < 30 * MS)
        message = refusal(feedback, recording=failed)
        assert "Channel 'left.value' has no Message at 30000000 ns" in message
        assert "incomplete" in message

    def test_archives_at_other_paths_author_another_manifest(
        self, tmp_path, feedback
    ):
        moved = tmp_path / "moved.fmu"
        moved.write_bytes(next(iter(feedthroughs(tmp_path).values()))
                          .read_bytes())
        message = refusal(feedback, fmus={"left": moved, "right": moved})
        assert "at the paths the Run named" in message

    def test_the_manifest_may_not_replace_an_input(self, feedback):
        with pytest.raises(AuthoringError, match="would replace it"):
            substitute(EXAMPLE / "feedback.json", feedthroughs(feedback.tmp_path),
                       "left", feedback.original, feedback.original,
                       feedback.tmp_path / "c.json")


def test_a_channel_no_retained_fmu_takes_is_left_out(tmp_path, sil_run):
    """`left` also publishes `left.probe`, which nothing takes."""
    document = copy.deepcopy(FEEDBACK)
    document["channels"]["left.probe"] = {
        **document["channels"]["left.value"], "subscribers": {}}
    replaced = Replaced(tmp_path, sil_run, write(tmp_path, document, "d.json"),
                        feedthroughs(tmp_path), "left")
    assert replaced.receipt["not_replayed"] == ["left.probe"]
    manifest = json.loads(replaced.manifest.read_text())
    assert "left.probe" not in manifest["channels"]
    assert replaced.compared(replaced.recording)["verdict"] == "pass"


# The command -------------------------------------------------------------------


class TestCommand:
    def arguments(self, feedback, **paths) -> list[str]:
        fmu = str(next(iter(feedthroughs(feedback.tmp_path).values())))
        return [str(EXAMPLE / "feedback.json"), "--fmu", "left", fmu,
                "--fmu", "right", fmu, "--replace", "left",
                "--recording", str(feedback.original),
                "-o", str(paths["out"]), "--contract", str(paths["contract"]),
                *(["--receipt", str(paths["receipt"])]
                  if "receipt" in paths else [])]

    def test_it_writes_the_manifest_the_contract_and_the_receipt(
        self, feedback, capsys
    ):
        paths = {name: feedback.tmp_path / f"cli-{name}.json"
                 for name in ("out", "contract", "receipt")}
        assert main(self.arguments(feedback, **paths)) == 0
        assert paths["out"].read_bytes() == feedback.manifest.read_bytes()
        assert paths["contract"].read_bytes() == \
            feedback.contract.read_bytes()
        receipt = json.loads(paths["receipt"].read_text())
        assert receipt["manifest"]["file"] == "cli-out.json"
        out = capsys.readouterr().out
        assert "replaced FMU 'left' by a Replay participant" in out
        assert "right.value (right): 5 Sample times" in out
        assert "unchanged retained experiment" in out

    def test_it_refuses_with_exit_2_and_writes_nothing(self, feedback, capsys):
        paths = {name: feedback.tmp_path / f"cli-{name}.json"
                 for name in ("out", "contract")}
        arguments = self.arguments(feedback, **paths)
        arguments[arguments.index("--replace") + 1] = "plant"
        assert main(arguments) == 2
        assert "sil-fmu-substitute: error:" in capsys.readouterr().err
        assert not any(path.exists() for path in paths.values())


def test_a_replaced_fmu_that_ran_last_publishes_at_its_own_place(
    tmp_path, sil_run
):
    """`right` runs after `left` in each Slot. Replayed first, its Message
    of a Slot would wait in `left`'s route of capacity 1 beside the one
    `left` has yet to take, and the route would overflow. The Replay
    participant publishes at `right`'s priority instead, so the Slot's
    Publish order is the live one."""
    replaced = Replaced(tmp_path, sil_run, EXAMPLE / "feedback.json",
                        feedthroughs(tmp_path), "right")
    replay = json.loads(replaced.manifest.read_text())["participants"]["right"]
    assert (replay["type"], replay["priority"]) == ("replay", 1)
    report = replaced.compared(replaced.recording)
    assert report["verdict"] == "pass", report["first_divergence"]
    assert [(t, channel) for channel, t, _ in read_records(replaced.recording)
            ] == [(t, channel) for channel, t, _ in
                  read_records(replaced.original)]
