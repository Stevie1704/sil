"""Generated scheduler regressions: complete ordered Activation traces (#404).

Each case runs a real Manifest through `sil-run`. The test compares the full
`(virtual_time_ns, participant, task)` sequence in stored Publish order with
the independent oracle in `scheduler_trace.py`. A failure names the case, its
input schedule, the first differing entry, and the command that reruns it.
"""

import json

import pytest
from conftest import run_manifest
from scheduler_trace import (
    FIXED_CASES,
    RUN_TIMEOUT_S,
    Case,
    NativeParticipant,
    ProcessParticipant,
    Task,
    assert_same_trace,
    corpus,
    expected_trace,
    first_divergence,
    generated_case,
    observed_trace,
    registration_entries,
    run_case,
    write_manifest,
)

CASES = corpus()


@pytest.mark.parametrize("case", CASES, ids=[c.case_id for c in CASES])
def test_runner_trace_matches_the_contract(sil_run, tmp_path, case):
    mcap = run_case(sil_run, case, tmp_path)
    assert_same_trace(case, expected_trace(case), observed_trace(case, mcap))


class TestOracle:
    """The oracle states the contract; these pin it on hand-checked cases."""

    def test_ties_break_on_name_sorted_registration(self):
        case = FIXED_CASES[0]
        assert [(e.participant, e.index) for e in registration_entries(case)] == [
            ("alpha", 0), ("bravo", 1), ("charlie", 2)]
        assert expected_trace(case)[:3] == [
            (0, "alpha", "tick"), (0, "bravo", "tick"), (0, "charlie", "tick")]

    def test_declared_task_order_breaks_ties_within_a_participant(self):
        case = FIXED_CASES[1]
        assert expected_trace(case)[:3] == [
            (0, "solo", "zulu"), (0, "solo", "alpha"), (0, "solo", "mike")]

    def test_duration_is_excluded_and_large_sums_do_not_wrap(self):
        case = Case("hand", 2**64 - 1, (NativeParticipant("p", (
            Task("a", 2**63, 0, 0), Task("b", 2**64 - 2, 2**64 - 2, 0))),))
        assert expected_trace(case) == [
            (0, "p", "a"), (2**63, "p", "a"), (2**64 - 2, "p", "b")]

    def test_generated_corpus_is_reproducible(self):
        assert generated_case(7) == generated_case(7)

    def test_corpus_covers_the_contract_edges(self):
        traces = {c.case_id: expected_trace(c) for c in CASES}
        times = {t for trace in traces.values() for t, _, _ in trace}
        assert 2**64 - 2 in times
        assert any(len({t for t, _, _ in trace}) < len(trace)
                   for trace in traces.values()), "no coincident Slots"
        assert any(any(isinstance(p, ProcessParticipant) for p in c.participants)
                   and any(isinstance(p, NativeParticipant)
                           for p in c.participants)
                   for c in CASES)


class TestObservation:
    """The trace can only show what the fixtures and the Manifest let through."""

    def test_manifest_keeps_the_authoring_order(self, tmp_path):
        # The tie case authors bravo before alpha; sil-run must see that.
        path = write_manifest(FIXED_CASES[0], tmp_path / "m.json")
        assert list(json.loads(path.read_text())["participants"]) == [
            "bravo", "alpha", "charlie"]

    # The interceptor drops every Publish at or after the duration, so a
    # wrong Activation there leaves no marker. The fixtures fail it instead.
    # The test lowers the fixture's bound to make the kernel cross it.
    @pytest.mark.parametrize("participant", [
        NativeParticipant("alpha", (Task("tick", 10, 0, 0),)),
        ProcessParticipant("alpha", 10, 0),
    ], ids=["native", "process"])
    def test_fixture_fails_an_activation_at_or_after_the_duration(
            self, sil_run, tmp_path, participant):
        case = Case("guard", 30, (participant,))
        path = write_manifest(case, tmp_path / "m.json")
        doc = json.loads(path.read_text())
        entry = doc["participants"]["alpha"]
        if isinstance(participant, NativeParticipant):
            entry["config"]["duration_ns"] = 20
        else:
            entry["command"][-1] = "20"
        path.write_text(json.dumps(doc))
        proc = run_manifest(sil_run, path, tmp_path / "o.mcap",
                            timeout=RUN_TIMEOUT_S)
        assert proc.returncode != 0
        assert "Activation at 20 ns is at or after the run duration" in (
            proc.stderr)


class TestDivergenceReport:
    def test_reports_reversed_ties(self):
        expected = [(0, "a", "t"), (0, "b", "t")]
        assert first_divergence(expected, expected[::-1]) == (
            "entry 0: expected (0, 'a', 't'), observed (0, 'b', 't')")

    def test_reports_early_end(self):
        assert first_divergence([(0, "a", "t")], []) == (
            "entry 0: observed trace ends early, expected (0, 'a', 't')")

    def test_reports_extra_entry(self):
        assert first_divergence([], [(5, "a", "t")]) == (
            "entry 0: observed trace has an extra entry (5, 'a', 't')")
