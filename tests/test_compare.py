"""Trajectory comparison under an explicit observation contract (issue #182).

Every expectation here is stated by hand — the observation time, field and
values of each divergence — never computed by the comparison's own
arithmetic. The ACC cases compare the retained SiL Recording of the
`plant-accelerate` qualification case against the independent fmpy trace of
the same FMU, converted by `sil-csv`.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest
from mcap.reader import make_reader
from mcap.writer import CompressionType, Writer

from conftest import ROOT
from sil.compare import ContractError, compare, main, read_contract, render
from sil.csv_recording import convert

EXAMPLE = ROOT / "examples" / "compare"
EVIDENCE = ROOT / "proofs" / "acc-fmi" / "evidence"
ACC_RECORDING = EVIDENCE / "plant-accelerate-1.mcap"
STEP_NS = 100_000_000

F64 = {"fields": [{"name": "v", "type": "f64"}]}
I32 = {"fields": [{"name": "n", "type": "i32"}]}


def write_recording(path: Path, schemas: dict, messages: list) -> Path:
    """An MCAP file shaped like the kernel's: one `sil_pod` schema per Channel.

    `schemas` maps a Channel to its schema spec; `messages` are
    `(channel, t_ns, payload)` in publish order.
    """
    with open(path, "wb") as f:
        writer = Writer(f, compression=CompressionType.NONE)
        writer.start(profile="sil", library="test")
        ids = {}
        for channel, spec in schemas.items():
            schema_id = writer.register_schema(
                channel, "sil_pod", json.dumps(spec, sort_keys=True).encode())
            ids[channel] = writer.register_channel(channel, "sil_pod", schema_id)
        for channel, t, payload in messages:
            writer.add_message(ids[channel], log_time=t, data=payload,
                               publish_time=t)
        writer.finish()
    return path


def f64(channel: str, t: int, value: float) -> tuple[str, int, bytes]:
    return channel, t, struct.pack("<d", value)


def contract(channels: dict, *, window=(0, 20)) -> dict:
    return {
        "sil_comparison": 1,
        "evaluation": {"from_ns": window[0], "to_ns": window[1]},
        "channels": channels,
    }


def float_channel(**overrides) -> dict:
    channel = {
        "actual_offset_ns": 0,
        "reference_offset_ns": 0,
        "observations": {"start_ns": 0, "stop_ns": 20, "step_ns": 10},
        "fields": {"v": {"atol": 0.5, "rtol": 0.25}},
    }
    channel.update(overrides)
    return channel


def run(tmp_path: Path, document: dict, actual: list, reference: list,
        *, actual_schemas=None, reference_schemas=None) -> dict:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(document))
    a = write_recording(tmp_path / "actual.mcap",
                        actual_schemas or {"c": F64}, actual)
    r = write_recording(tmp_path / "reference.mcap",
                        reference_schemas or {"c": F64}, reference)
    return compare(read_contract(path), a, r)


def series(values: dict[int, float], channel: str = "c") -> list:
    return [f64(channel, t, v) for t, v in values.items()]


# -- tolerance and value rules ------------------------------------------------

def test_the_tolerance_is_atol_plus_rtol_times_the_reference(tmp_path):
    # Reference 4: allowed = 0.5 + 0.25 * 4 = 1.5, so 5.5 is exactly on it.
    report = run(tmp_path, contract({"c": float_channel()}),
                 series({0: 5.5, 10: 2.5, 20: -2.0}),
                 series({0: 4.0, 10: 4.0, 20: -4.0}))

    assert report["verdict"] == "fail"
    divergence = report["first_divergence"]
    assert divergence["kind"] == "value"
    # 10: |2.5 - 4| = 1.5 is on the bound; 20: |−2 − −4| = 2 > 0.5 + 1.
    assert divergence["observation_ns"] == 20
    assert divergence["actual"] == -2.0
    assert divergence["expected"] == -4.0
    assert divergence["abs_error"] == 2.0
    assert divergence["tolerance"] == {"atol": 0.5, "rtol": 0.25, "allowed": 1.5}
    assert report["channels"]["c"]["checked"] == 3
    assert report["channels"]["c"]["failed"] == 1


def test_a_non_finite_value_fails_on_either_side(tmp_path):
    report = run(tmp_path, contract({"c": float_channel()}),
                 series({0: math.nan, 10: math.inf, 20: 1.0}),
                 series({0: math.nan, 10: math.inf, 20: math.nan}))

    assert report["channels"]["c"]["nonfinite"] == 3
    divergence = report["first_divergence"]
    assert divergence["kind"] == "nonfinite"
    assert divergence["observation_ns"] == 0
    assert (divergence["actual"], divergence["expected"]) == ("nan", "nan")


def test_an_integer_field_is_compared_exactly(tmp_path):
    document = contract({"c": float_channel(fields={"n": "exact"})})
    report = run(tmp_path, document,
                 [("c", 0, struct.pack("<i", 7)), ("c", 10, struct.pack("<i", 8)),
                  ("c", 20, struct.pack("<i", 9))],
                 [("c", 0, struct.pack("<i", 7)), ("c", 10, struct.pack("<i", 9)),
                  ("c", 20, struct.pack("<i", 9))],
                 actual_schemas={"c": I32}, reference_schemas={"c": I32})

    divergence = report["first_divergence"]
    assert (divergence["kind"], divergence["observation_ns"]) == ("value", 10)
    assert (divergence["actual"], divergence["expected"]) == (8, 9)
    assert divergence["tolerance"] == "exact"


def test_an_ignored_field_is_named_and_never_compared(tmp_path):
    two = {"fields": [{"name": "v", "type": "f64"}, {"name": "w", "type": "f64"}]}
    document = contract({"c": float_channel(
        fields={"v": {"atol": 0, "rtol": 0}, "w": "ignore"})})
    pack = struct.Struct("<dd").pack
    report = run(tmp_path, document,
                 [("c", t, pack(1.0, math.nan)) for t in (0, 10, 20)],
                 [("c", t, pack(1.0, 5.0)) for t in (0, 10, 20)],
                 actual_schemas={"c": two}, reference_schemas={"c": two})

    assert report["verdict"] == "pass"


def test_an_overflowing_error_is_stated_in_strict_json(tmp_path, capsys):
    report = run(tmp_path, contract({"c": float_channel()}),
                 series({0: 1e308, 10: 0.0, 20: 0.0}),
                 series({0: -1e308, 10: 0.0, 20: 0.0}))

    assert report["first_divergence"]["abs_error"] == "inf"
    main([str(tmp_path / "contract.json"), str(tmp_path / "actual.mcap"),
          str(tmp_path / "reference.mcap"), "--json"])
    assert strict_json(capsys.readouterr().out)["verdict"] == "fail"


def test_a_channel_that_compares_no_field_fails_coverage(tmp_path):
    report = run(tmp_path, contract({"c": float_channel(fields={"v": "ignore"})}),
                 series({0: 0.0, 10: 1.0, 20: 2.0}),
                 series({0: 0.0, 10: 1.0, 20: 2.0}))

    assert report["verdict"] == "fail"
    assert report["coverage"] == ["Channel 'c' compares no field"]


# -- observation times --------------------------------------------------------

def test_nothing_is_interpolated_between_messages(tmp_path):
    # The reference has 0 and 20; the actual only 10. A straight line would
    # match; the comparison states three missing Messages instead.
    report = run(tmp_path, contract({"c": float_channel()}),
                 series({10: 1.0}), series({0: 0.0, 20: 2.0}))

    counts = report["channels"]["c"]
    assert (counts["checked"], counts["missing_actual"],
            counts["missing_reference"]) == (0, 2, 1)
    divergence = report["first_divergence"]
    assert (divergence["kind"], divergence["observation_ns"]) == ("missing-actual", 0)
    assert divergence["reference_time_ns"] == 0
    assert divergence["expected"] == {"v": 0.0}
    assert divergence["actual"] is None


def test_a_message_off_the_observation_grid_is_not_observed(tmp_path):
    report = run(tmp_path, contract({"c": float_channel()}),
                 series({0: 0.0, 5: 99.0, 10: 1.0, 20: 2.0}),
                 series({0: 0.0, 10: 1.0, 15: -99.0, 20: 2.0}))

    assert report["verdict"] == "pass"
    assert report["channels"]["c"]["checked"] == 3


def test_duplicate_messages_at_one_observation_are_ambiguous(tmp_path):
    report = run(tmp_path, contract({"c": float_channel()}),
                 series({0: 0.0, 10: 1.0, 20: 2.0}) + [f64("c", 10, 1.0)],
                 series({0: 0.0, 10: 1.0, 20: 2.0}))

    assert report["channels"]["c"]["ambiguous_actual"] == 1
    divergence = report["first_divergence"]
    assert (divergence["kind"], divergence["observation_ns"]) == ("ambiguous-actual", 10)


def test_each_channel_maps_publication_to_observation_with_its_own_offset(tmp_path):
    # Channel a publishes a state at t that holds at t + 10; channel b holds
    # at its publication time. The reference states both at observation time.
    document = contract({
        "a": float_channel(actual_offset_ns=10,
                           observations={"times_ns": [10, 20]}),
        "b": float_channel(observations={"times_ns": [10, 20]}),
    })
    report = run(tmp_path, document,
                 series({0: 1.0, 10: 2.0}, "a") + series({10: 3.0, 20: 4.0}, "b"),
                 series({10: 1.0, 20: 2.0}, "a") + series({10: 3.0, 20: 4.0}, "b"),
                 actual_schemas={"a": F64, "b": F64},
                 reference_schemas={"a": F64, "b": F64})

    assert report["verdict"] == "pass", report["first_divergence"]
    assert report["channels"]["a"]["checked"] == 2
    assert report["channels"]["b"]["checked"] == 2


def test_the_evaluation_window_leaves_the_warm_up_unobserved(tmp_path):
    report = run(tmp_path, contract({"c": float_channel()}, window=(10, 20)),
                 series({0: 99.0, 10: 1.0, 20: 2.0}),
                 series({0: 0.0, 10: 1.0, 20: 2.0}))

    assert report["verdict"] == "pass"
    assert report["channels"]["c"]["observations"] == 2


def test_the_first_divergence_is_the_earliest_observation(tmp_path):
    document = contract({"a": float_channel(), "b": float_channel()})
    report = run(tmp_path, document,
                 series({0: 0.0, 10: 1.0, 20: 99.0}, "a")
                 + series({0: 0.0, 10: 99.0, 20: 2.0}, "b"),
                 series({0: 0.0, 10: 1.0, 20: 2.0}, "a")
                 + series({0: 0.0, 10: 1.0, 20: 2.0}, "b"),
                 actual_schemas={"a": F64, "b": F64},
                 reference_schemas={"a": F64, "b": F64})

    divergence = report["first_divergence"]
    assert (divergence["channel"], divergence["observation_ns"]) == ("b", 10)
    assert report["divergences"] == 2


# -- coverage -----------------------------------------------------------------

@pytest.mark.parametrize("fields, problem", [
    ({}, "does not name field 'v'"),
    ({"v": "ignore", "x": "ignore"}, "names field 'x'"),
    ({"v": "exact"}, "'exact' does not fit f64 field 'v'"),
])
def test_wrong_field_coverage_fails(tmp_path, fields, problem):
    report = run(tmp_path, contract({"c": float_channel(fields=fields)}),
                 series({0: 0.0, 10: 1.0, 20: 2.0}),
                 series({0: 0.0, 10: 1.0, 20: 2.0}))

    assert report["verdict"] == "fail"
    assert any(problem in entry for entry in report["coverage"]), report["coverage"]


def test_a_tolerance_on_an_integer_field_fails_coverage(tmp_path):
    document = contract({"c": float_channel(fields={"n": {"atol": 1, "rtol": 0}})})
    report = run(tmp_path, document, [], [],
                 actual_schemas={"c": I32}, reference_schemas={"c": I32})

    assert any("tolerance does not fit i32 field 'n'" in entry
               for entry in report["coverage"]), report["coverage"]


def test_a_channel_absent_from_a_recording_fails_coverage(tmp_path):
    document = contract({"c": float_channel(reference_channel="r")})
    report = run(tmp_path, document, series({0: 0.0}), series({0: 0.0}))

    assert report["verdict"] == "fail"
    assert report["coverage"] == [
        "reference Recording has no Channel 'r' (compared with actual 'c')"
    ]


# -- the contract document ------------------------------------------------------

@pytest.mark.parametrize("change, problem", [
    (lambda d: d.update(sil_comparison=2), "sil_comparison"),
    (lambda d: d.update(extra=1), "unknown key 'extra'"),
    (lambda d: d.pop("evaluation"), "'evaluation'"),
    (lambda d: d["channels"]["c"].pop("actual_offset_ns"), "actual_offset_ns"),
    (lambda d: d["channels"]["c"]["observations"].update(times_ns=[0]),
     "observations"),
    (lambda d: d["channels"]["c"].update(observations={"times_ns": [10, 0]}),
     "ascending"),
    (lambda d: d["channels"]["c"].update(
        observations={"start_ns": 0, "stop_ns": 20, "step_ns": 0}), "step_ns"),
    (lambda d: d["channels"]["c"].update(fields={"v": {"atol": -1, "rtol": 0}}),
     "atol"),
    (lambda d: d["channels"]["c"].update(fields={"v": {"atol": True, "rtol": 0}}),
     "atol"),
    (lambda d: d["channels"]["c"].update(fields={"v": {"atol": 1}}), "rtol"),
    (lambda d: d["channels"]["c"].update(fields={"v": "close"}), "'close'"),
    (lambda d: d["channels"]["c"].update(
        observations={"times_ns": [30, 40]}), "nothing inside"),
    (lambda d: d.update(channels={}), "no Channel"),
])
def test_a_malformed_contract_is_refused(tmp_path, change, problem):
    document = contract({"c": float_channel()})
    change(document)
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ContractError, match=problem):
        read_contract(path)


def test_a_repeated_key_is_refused(tmp_path):
    path = tmp_path / "contract.json"
    text = json.dumps(contract({"c": float_channel()}))
    path.write_text(text.replace('"fields": {', '"fields": {"v": "ignore", ', 1))

    with pytest.raises(ContractError, match="repeats key 'v'"):
        read_contract(path)


def test_an_unreadable_contract_is_refused(tmp_path):
    path = tmp_path / "contract.json"
    path.write_text("{")
    with pytest.raises(ContractError, match="not JSON"):
        read_contract(path)
    with pytest.raises(ContractError, match="cannot read"):
        read_contract(tmp_path / "absent.json")


# -- the ACC evidence -----------------------------------------------------------

def fmpy_reference_rows() -> list[list[str]]:
    """The fmpy trace pivoted by hand: one row per time, instance 0 then 1."""
    import csv

    names = ["ego_position_m", "ego_speed_mps", "lead_position_m",
             "lead_speed_mps", "gap_m", "relative_speed_mps"]
    by_time: dict[str, dict[str, list[str]]] = {}
    with open(EVIDENCE / "plant-accelerate.fmpy.csv", newline="") as f:
        for row in csv.DictReader(f):
            if row["phase"] in ("initialized", "step"):
                by_time.setdefault(row["time"], {})[row["instance"]] = [
                    row[f"output.{name}"] for name in names]
    return [[t, *instances["0"], *instances["1"]] for t, instances in by_time.items()]


def test_the_example_reference_is_the_retained_fmpy_trace():
    import csv

    with open(EXAMPLE / "plant-accelerate.reference.csv", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[1:] == fmpy_reference_rows()


@pytest.fixture
def acc_reference(tmp_path) -> Path:
    out = tmp_path / "reference.mcap"
    convert(EXAMPLE / "reference-mapping.json",
            EXAMPLE / "plant-accelerate.reference.csv", out)
    return out


def acc_compare(actual: Path, reference: Path) -> dict:
    return compare(read_contract(EXAMPLE / "contract.json"), actual, reference)


def rewrite(source: Path, out: Path, edit) -> Path:
    """A copy of `source` with `edit(channel, t, payload)` applied per Message;
    it returns a list of replacement `(channel, t, payload)` tuples."""
    schemas, messages = {}, []
    with open(source, "rb") as f:
        for schema, channel, message in make_reader(f).iter_messages():
            schemas[channel.topic] = json.loads(schema.data)
            messages += edit(channel.topic, message.log_time, message.data)
    return write_recording(out, schemas, messages)


def with_field(payload: bytes, index: int, value: float) -> bytes:
    values = list(struct.unpack("<6d", payload))
    values[index] = value
    return struct.pack("<6d", *values)


def test_the_retained_sil_run_matches_the_independent_trace(acc_reference):
    report = acc_compare(ACC_RECORDING, acc_reference)

    assert report["verdict"] == "pass", report["first_divergence"]
    assert report["coverage"] == []
    for channel in ("output0", "output1"):
        counts = report["channels"][channel]
        # 100 ms .. 1 s: the tenth observation comes from the final
        # publication at 900 ms, which no in-run participant consumes.
        assert counts["observations"] == counts["checked"] == 10
        assert counts["missing_actual"] == counts["missing_reference"] == 0


def test_a_late_channel_is_a_timing_divergence(acc_reference, tmp_path):
    late = rewrite(ACC_RECORDING, tmp_path / "late.mcap",
                   lambda c, t, p: [(c, t + STEP_NS if c == "output1" else t, p)])
    report = acc_compare(late, acc_reference)

    divergence = report["first_divergence"]
    assert (divergence["kind"], divergence["channel"],
            divergence["observation_ns"]) == ("missing-actual", "output1", 100_000_000)
    assert report["channels"]["output0"]["failed"] == 0
    # Every later observation holds the previous Step's state.
    assert report["channels"]["output1"]["failed"] == 9


def test_a_wrong_value_names_field_times_and_values(acc_reference, tmp_path):
    wrong = rewrite(
        ACC_RECORDING, tmp_path / "wrong.mcap",
        lambda c, t, p: [(c, t, with_field(p, 1, 25.749999999999993 + 1e-6)
                          if (c, t) == ("output0", 400_000_000) else p)])
    report = acc_compare(wrong, acc_reference)

    divergence = report["first_divergence"]
    assert divergence["kind"] == "value"
    assert (divergence["channel"], divergence["reference_channel"],
            divergence["field"]) == ("output0", "plant0", "ego_speed_mps")
    assert divergence["observation_ns"] == 500_000_000
    assert divergence["actual_publication_ns"] == 400_000_000
    assert divergence["reference_time_ns"] == 500_000_000
    assert divergence["expected"] == 25.749999999999993
    assert divergence["actual"] == 25.749999999999993 + 1e-6
    assert report["divergences"] == 1


def test_a_missing_final_publication_is_a_divergence(acc_reference, tmp_path):
    short = rewrite(ACC_RECORDING, tmp_path / "short.mcap",
                    lambda c, t, p: [] if t == 900_000_000 else [(c, t, p)])
    report = acc_compare(short, acc_reference)

    divergence = report["first_divergence"]
    assert (divergence["kind"], divergence["channel"],
            divergence["observation_ns"]) == ("missing-actual", "output0", 1_000_000_000)
    assert divergence["expected"]["ego_position_m"] == 25.75
    for channel in ("output0", "output1"):
        assert report["channels"][channel]["checked"] == 9
        assert report["channels"][channel]["missing_actual"] == 1


def test_an_unexpected_nan_is_a_divergence(acc_reference, tmp_path):
    broken = rewrite(
        ACC_RECORDING, tmp_path / "nan.mcap",
        lambda c, t, p: [(c, t, with_field(p, 4, math.nan)
                          if (c, t) == ("output1", 200_000_000) else p)])
    report = acc_compare(broken, acc_reference)

    divergence = report["first_divergence"]
    assert (divergence["kind"], divergence["channel"], divergence["field"],
            divergence["observation_ns"]) == ("nonfinite", "output1", "gap_m",
                                              300_000_000)
    assert (divergence["actual"], divergence["expected"]) == ("nan", 60.135)


# -- the report and the command -------------------------------------------------

def test_the_report_states_it_is_not_a_determinism_check(acc_reference):
    report = acc_compare(ACC_RECORDING, acc_reference)

    assert report["sil_comparison_report"] == 1
    assert report["comparison"] == "reference"
    assert "determinism" in report["determinism"]
    assert "not a determinism check" in render(report)


def strict_json(text: str):
    def refuse(token):
        raise ValueError(f"non-standard JSON token {token}")
    return json.loads(text, parse_constant=refuse)


def test_the_command_exit_status_is_the_verdict(acc_reference, tmp_path, capsys):
    contract_path = str(EXAMPLE / "contract.json")
    assert main([contract_path, str(ACC_RECORDING), str(acc_reference), "--json"]) == 0
    report = strict_json(capsys.readouterr().out)
    assert report["verdict"] == "pass"

    broken = rewrite(
        ACC_RECORDING, tmp_path / "nan.mcap",
        lambda c, t, p: [(c, t, with_field(p, 4, math.nan)
                          if (c, t) == ("output1", 200_000_000) else p)])
    assert main([contract_path, str(broken), str(acc_reference), "--json"]) == 1
    assert strict_json(capsys.readouterr().out)["first_divergence"]["actual"] == "nan"

    assert main([contract_path, str(broken), str(acc_reference)]) == 1
    readable = capsys.readouterr().out
    assert "verdict: fail" in readable
    assert "first divergence: nonfinite output1.gap_m at observation 300000000 ns" in readable


def test_the_command_refuses_unreadable_inputs(acc_reference, tmp_path, capsys):
    contract_path = str(EXAMPLE / "contract.json")
    assert main([contract_path, str(tmp_path / "absent.mcap"), str(acc_reference)]) == 2
    assert "sil-compare:" in capsys.readouterr().err
    csv = tmp_path / "actual.csv"
    csv.write_text("t\n")
    assert main([contract_path, str(csv), str(acc_reference)]) == 2
    assert "recording format" in capsys.readouterr().err
    assert main([str(tmp_path / "absent.json"), str(ACC_RECORDING),
                 str(acc_reference)]) == 2


def test_the_readme_documents_the_example():
    readme = (ROOT / "README.md").read_text()
    for name in ("contract.json", "reference-mapping.json",
                 "plant-accelerate.reference.csv"):
        assert f"examples/compare/{name}" in readme
