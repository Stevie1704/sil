"""The opt-in diagnostics runner and its `sil_run_diagnostics` v1 report (#300).

`sil-run-diagnostics` is the kernel built with the collector compiled in. These
tests run it at the run boundary and read the report it writes. Timing values
vary per Run, so no assertion here depends on how long anything took: they pin
identities, counts, structure, null reasons and the outcome.
"""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import BUILD_DIR, COMPAT_ROUTE_CAPACITY, ROOT
from sil.diagnostics_report import validate_run_report
from sil.manifest import SubscriberRoute
from toys import add_producer, bounded_route_manifest, toy_manifest

MS = 1_000_000


def run_diagnostics(runner: Path, manifest: Path, tmp_path: Path, *args: str,
                    report: Path | None = None, launcher: Path | None = None):
    """One diagnostics Run; returns the process and the parsed report.

    Pass `launcher` when the runner's own peak RSS is asserted: on Linux a
    runner started directly from pytest inherits pytest's high-water mark.
    """
    report = report or tmp_path / "run.json"
    command = [str(runner), str(manifest), "--diagnostics-out", str(report),
               *args]
    if launcher is not None:
        command.insert(0, str(launcher))
    if "--no-recording" not in args and "-o" not in args:
        command += ["-o", str(tmp_path / "run.mcap")]
    # cwd: --no-recording still writes provenance beside the default out.mcap.
    proc = subprocess.run(command, capture_output=True, text=True, cwd=tmp_path)
    document = json.loads(report.read_text()) if report.exists() else None
    return proc, document


def add_multitask(m, name="multi", *, channel="ticks", **config):
    m.add_native(
        name,
        library=str(BUILD_DIR / "toy_multitask.silp"),
        config={"channel": channel, **config},
        publishes=[channel],
    )


def mixed_period_manifest(tmp_path: Path) -> Path:
    """Two Native Tasks at 1 ms and 5 ms, and a Process Step at 10 ms."""
    m = toy_manifest(duration_ns=20 * MS)
    m.add_channel("ticks", schema="toy.Counter")
    m.add_channel("echo", schema="toy.Counter")
    add_multitask(m, fast_period_ns=1 * MS, slow_period_ns=5 * MS)
    m.add_process(
        "pyecho",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "echo.py")],
        step_period_ns=10 * MS,
        subscribes=[SubscriberRoute("ticks", capacity=COMPAT_ROUTE_CAPACITY)],
        publishes=["echo"],
    )
    return m.write(tmp_path / "mixed.json").path


def task_rows(document):
    return {
        (participant["participant"], task["task"]): task
        for participant in document["execution"]["participants"]
        for task in participant["tasks"]
    }


def timing_rows(document):
    return {
        (row["participant"], row["task"]): row
        for row in document["observational"]["tasks"]
    }


class TestIdentitiesAndCounts:
    def test_every_task_is_identified_with_exact_counts(
        self, sil_run_diagnostics, tmp_path
    ):
        proc, report = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path
        )
        assert proc.returncode == 0, proc.stderr
        assert report["sil_run_diagnostics"] == 1
        assert report["run"]["exit_code"] == 0
        rows = task_rows(report)
        assert set(rows) == {
            ("multi", "fast"), ("multi", "slow"), ("pyecho", "step")
        }
        expected = {
            ("multi", "fast"): ("native_task", 1 * MS, 20),
            ("multi", "slow"): ("native_task", 5 * MS, 4),
            ("pyecho", "step"): ("process_step", 10 * MS, 2),
        }
        for key, (kind, period, count) in expected.items():
            row = rows[key]
            assert row["kind"] == kind
            assert row["period_ns"] == period
            assert row["attempted"] == row["completed"] == count
            assert row["failed"] == row["interrupted"] == 0
        kinds = {
            p["participant"]: p["kind"]
            for p in report["execution"]["participants"]
        }
        assert kinds == {"multi": "native", "pyecho": "process"}
        assert validate_run_report(report) == []

    def test_process_and_native_timing_name_their_inclusive_scope(
        self, sil_run_diagnostics, tmp_path
    ):
        _, report = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path
        )
        timing = timing_rows(report)
        # A Process Step includes codec work, IPC, OS waiting, the Participant
        # and synchronous routing/recording; it is not algorithm time.
        assert timing[("pyecho", "step")]["timing"]["scope"] == (
            "inclusive_process_step"
        )
        assert timing[("multi", "fast")]["timing"]["scope"] == (
            "inclusive_native_activation"
        )
        for row in timing.values():
            assert row["timing"]["completed"] == (
                task_rows(report)[(row["participant"], row["task"])]["completed"]
            )

    def test_replay_work_is_counted_apart_from_tasks(
        self, sil_run, sil_run_diagnostics, tmp_path
    ):
        recording = tmp_path / "record.mcap"
        source = toy_manifest(duration_ns=50 * MS)
        source.add_channel("ticks", schema="toy.Counter")
        add_producer(source, "aprod", channel="ticks", period_ns=10 * MS)
        recorded = subprocess.run(
            [str(sil_run), str(source.write(tmp_path / "src.json").path),
             "-o", str(recording)],
            capture_output=True, text=True,
        )
        assert recorded.returncode == 0, recorded.stderr
        m = toy_manifest(duration_ns=50 * MS)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_replay("rep", recording=str(recording), channels=["ticks"])
        proc, report = run_diagnostics(
            sil_run_diagnostics, m.write(tmp_path / "replay.json").path,
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        (participant,) = report["execution"]["participants"]
        assert participant["kind"] == "replay"
        assert participant["tasks"] == []
        # Five recorded Messages at 0..40 ms, one per publication Slot.
        assert participant["replay"]["publications"] == 5
        assert participant["replay"]["attempted"] == 5
        assert participant["replay"]["completed"] == 5
        assert report["observational"]["tasks"] == []
        (replay,) = report["observational"]["replay"]
        assert replay["timing"]["scope"] == "inclusive_replay_publication_slot"
        assert replay["timing"]["completed"] == 5
        throughput = report["observational"]["throughput"]
        assert throughput["completed_native_activations"] == 0
        assert throughput["completed_process_steps"] == 0
        assert validate_run_report(report) == []


class TestLifecycle:
    def test_phases_account_for_the_run_wall_time(
        self, sil_run_diagnostics, tmp_path
    ):
        _, report = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path
        )
        wall = report["observational"]["run_wall_time"]["wall_ns"]
        phases = report["observational"]["phases"]
        named = [
            phases[name] for name in (
                "preflight_ns", "setup_ns", "scheduled_execution_ns",
                "shutdown_ns", "recording_finalization_ns",
            )
        ]
        assert all(type(value) is int and value >= 0 for value in named)
        assert sum(named) + phases["unattributed_ns"] == wall
        assert phases["unavailable"] == {}

    def test_no_recording_has_no_finalization_phase(
        self, sil_run_diagnostics, tmp_path
    ):
        proc, report = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path,
            "--no-recording",
        )
        assert proc.returncode == 0, proc.stderr
        phases = report["observational"]["phases"]
        assert phases["recording_finalization_ns"] is None
        assert phases["unavailable"] == {
            "recording_finalization_ns": "no_recording"
        }
        assert report["configuration"]["recording"] is False
        assert report["artifacts"]["recording"] is None
        assert validate_run_report(report) == []

    def test_successful_run_reports_speed_and_throughput(
        self, sil_run_diagnostics, tmp_path
    ):
        _, report = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path
        )
        speed = report["observational"]["virtual_time_speed"]
        phases = report["observational"]["phases"]
        wall = report["observational"]["run_wall_time"]["wall_ns"]
        assert speed["scheduled_factor"] == pytest.approx(
            20 * MS / phases["scheduled_execution_ns"]
        )
        assert speed["end_to_end_factor"] == pytest.approx(20 * MS / wall)
        throughput = report["observational"]["throughput"]
        assert throughput["completed_native_activations"] == 24
        assert throughput["completed_process_steps"] == 2
        assert throughput["scheduled_execution_ns"] == (
            phases["scheduled_execution_ns"]
        )
        assert throughput["process_steps_per_s"] == pytest.approx(
            2 / (phases["scheduled_execution_ns"] / 1e9)
        )
        assert report["execution"]["last_virtual_time_ns"] == 19 * MS


class TestNullSemantics:
    def test_zero_activations_have_reasons_not_zeros(
        self, sil_run_diagnostics, tmp_path
    ):
        # The only Task's first activation lies beyond the Duration.
        m = toy_manifest(duration_ns=10 * MS)
        m.add_channel("ticks", schema="toy.Counter")
        add_producer(m, "late", channel="ticks", period_ns=MS,
                     offset_ns=20 * MS)
        proc, report = run_diagnostics(
            sil_run_diagnostics, m.write(tmp_path / "late.json").path, tmp_path
        )
        assert proc.returncode == 0, proc.stderr
        assert task_rows(report)[("late", "tick")]["attempted"] == 0
        timing = timing_rows(report)[("late", "tick")]["timing"]
        assert timing["completed"] == 0
        for key in ("min_ns", "max_ns", "mean_ns", "stddev_ns", "p50_ns",
                    "p95_ns", "p99_ns"):
            assert timing[key] is None
            assert timing["unavailable"][key] == "no_completed_activations"
        assert timing["samples_ns"] == [] and timing["slowest"] == []
        assert report["execution"]["last_virtual_time_ns"] is None
        assert report["execution"]["unavailable"] == {
            "last_virtual_time_ns": "no_slot_opened"
        }
        assert validate_run_report(report) == []


MEMORY_PEAK = ROOT / "tests" / "participants" / "memory_peak.py"
RESOURCES_SUPPORTED = sys.platform in ("linux", "darwin")
requires_resources = pytest.mark.skipif(
    not RESOURCES_SUPPORTED, reason="resource collection needs Linux or macOS"
)


def resource_records(document):
    resources = document["observational"]["resources"]
    by_participant = {r["participant"]: r for r in resources["process_participants"]}
    return resources, resources["runner"], by_participant


@requires_resources
class TestResources:
    def test_runner_record_is_self_accounting_only(
        self, sil_run_diagnostics, tmp_path
    ):
        proc, report = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path
        )
        assert proc.returncode == 0, proc.stderr
        assert report["collector"]["collector_version"] == 2
        _, runner, _ = resource_records(report)
        assert runner["scope"] == "runner_process"
        assert runner["collector"] == "getrusage_self"
        assert runner["interval"] == "runner_start_to_run_end"
        # RUSAGE_SELF never includes the Process participants it reaped.
        assert runner["includes_waited_descendants"] is False
        assert runner["peak_rss_kind"] == "process_peak_rss"
        assert runner["unavailable"] == {}
        assert runner["peak_rss_bytes"] > 0
        assert isinstance(runner["user_cpu_ns"], int)
        assert isinstance(runner["system_cpu_ns"], int)
        assert validate_run_report(report) == []

    def test_each_process_participant_is_its_own_waited_scope(
        self, sil_run_diagnostics, tmp_path
    ):
        _, report = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path
        )
        resources, _, processes = resource_records(report)
        # Native Tasks run inside the runner, so they share its record.
        assert resources["native_participants"] == "shared_with_runner"
        assert set(processes) == {"pyecho"}
        record = processes["pyecho"]
        assert record["scope"] == "process_participant"
        assert record["collector"] == "wait4_child"
        assert record["interval"] == "spawn_to_reap"
        assert record["includes_waited_descendants"] is True
        assert record["peak_rss_kind"] == "largest_process_peak_rss"
        assert record["unavailable"] == {}
        assert record["peak_rss_bytes"] > 0
        assert "descendants_counted_only_when_waited" in record["limitations"]

    def test_cpu_total_adds_only_disjoint_scopes(
        self, sil_run_diagnostics, tmp_path
    ):
        _, report = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path
        )
        resources, runner, processes = resource_records(report)
        total = resources["cpu_total"]
        assert total["scope"] == "runner_and_process_participants"
        for key in ("user_cpu_ns", "system_cpu_ns"):
            assert total[key] == runner[key] + sum(
                r[key] for r in processes.values())

    def test_no_member_sums_peak_memory(self, sil_run_diagnostics, tmp_path):
        """Peaks of different processes are never added into a total."""
        _, report = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path
        )
        resources = report["observational"]["resources"]
        assert set(resources) == {
            "runner", "process_participants", "native_participants",
            "cpu_total",
        }
        assert not any("rss" in key for key in resources["cpu_total"])

    def test_peaks_at_different_times_stay_separate(
        self, sil_run_diagnostics, sil_diagnostics_launcher, tmp_path
    ):
        """Two 64 MiB peaks, 30 ms apart: each record has its own, none adds
        them, and no member claims the Run held 128 MiB at once."""
        mib = 64
        m = toy_manifest(duration_ns=50 * MS)
        for name, peak_at in (("early", 0), ("late", 30 * MS)):
            m.add_process(
                name,
                command=[sys.executable, str(MEMORY_PEAK), str(peak_at),
                         str(mib)],
                step_period_ns=10 * MS,
            )
        proc, report = run_diagnostics(
            sil_run_diagnostics, m.write(tmp_path / "peaks.json").path,
            tmp_path, launcher=sil_diagnostics_launcher,
        )
        assert proc.returncode == 0, proc.stderr
        assert validate_run_report(report) == []
        resources, runner, processes = resource_records(report)
        assert set(processes) == {"early", "late"}
        for record in processes.values():
            assert record["peak_rss_kind"] == "largest_process_peak_rss"
            assert record["peak_rss_bytes"] >= mib * 2**20
            # The other Participant's peak is not in this one.
            assert record["peak_rss_bytes"] < 2 * mib * 2**20
        # Children's memory is not the runner's.
        assert runner["peak_rss_bytes"] < mib * 2**20
        assert "cpu_total" in resources
        assert not any("rss" in key for key in resources["cpu_total"])

    def test_short_lived_failing_participant_keeps_its_evidence(
        self, sil_run_diagnostics, tmp_path
    ):
        m = toy_manifest(duration_ns=50 * MS)
        m.add_process(
            "quitter",
            command=[sys.executable, "-c", "import sys; sys.exit(3)"],
            step_period_ns=10 * MS,
        )
        proc, report = run_diagnostics(
            sil_run_diagnostics, m.write(tmp_path / "quitter.json").path,
            tmp_path,
        )
        assert proc.returncode == 1
        assert "exited unexpectedly with status 3" in proc.stderr
        assert "status 3" in report["run"]["failure"]["message"]
        _, _, processes = resource_records(report)
        record = processes["quitter"]
        assert record["unavailable"] == {}
        assert record["peak_rss_bytes"] > 0
        assert record["user_cpu_ns"] + record["system_cpu_ns"] > 0
        assert validate_run_report(report) == []

    def test_failed_step_participant_keeps_its_evidence(
        self, sil_run_diagnostics, tmp_path
    ):
        proc, report = run_diagnostics(
            sil_run_diagnostics, failing_step_manifest(tmp_path), tmp_path
        )
        assert proc.returncode == 1
        assert "boom" in report["run"]["failure"]["message"]
        _, _, processes = resource_records(report)
        assert processes["test"]["peak_rss_bytes"] > 0


def failing_step_manifest(tmp_path: Path) -> Path:
    m = toy_manifest(duration_ns=50 * MS)
    m.add_process(
        "test",
        command=[sys.executable,
                 str(ROOT / "tests" / "participants" / "fail_at_20ms.py")],
        step_period_ns=10 * MS,
    )
    return m.write(tmp_path / "failing.json").path


TIMEOUT_PARTICIPANT = ROOT / "tests" / "participants" / "timeout.py"


def wedged_manifest(tmp_path: Path, observer: Path | None = None) -> Path:
    m = toy_manifest(duration_ns=50 * MS)
    command = [sys.executable, str(TIMEOUT_PARTICIPANT), "step"]
    if observer is not None:
        command.append(str(observer))
    m.add_process("wedged", command=command, step_period_ns=10 * MS)
    return m.write(tmp_path / "wedged.json").path


class TestFailedRuns:
    def test_failed_step_gives_honest_partial_data(
        self, sil_run_diagnostics, tmp_path
    ):
        proc, report = run_diagnostics(
            sil_run_diagnostics, failing_step_manifest(tmp_path), tmp_path
        )
        assert proc.returncode == 1
        assert report["run"]["exit_code"] == 1
        assert report["run"]["status"] == "run_failure"
        assert report["run"]["failure"]["participant"] == "test"
        assert "boom" in report["run"]["failure"]["message"]
        assert report["completeness"] == {
            "run_complete": False, "reasons": ["run_failure"]
        }
        step = task_rows(report)[("test", "step")]
        # Steps at 0 and 10 ms completed; the one at 20 ms failed.
        assert (step["attempted"], step["completed"], step["failed"]) == (3, 2, 1)
        assert step["failed_activation"] == {
            "activation_index": 2, "virtual_time_ns": 20 * MS,
            "outcome": "failed",
        }
        timing = timing_rows(report)[("test", "step")]
        assert timing["timing"]["completed"] == 2
        assert timing["failed_activation"]["activation_index"] == 2
        assert report["execution"]["last_virtual_time_ns"] == 20 * MS
        speed = report["observational"]["virtual_time_speed"]
        assert speed["scheduled_factor"] is None
        assert speed["end_to_end_factor"] is None
        assert speed["unavailable"] == {
            "scheduled_factor": "run_not_successful",
            "end_to_end_factor": "run_not_successful",
        }
        assert report["artifacts"]["failure_report"].endswith(
            "run.mcap.failure.json"
        )
        assert validate_run_report(report) == []

    def test_failed_native_activation_is_attributed(
        self, sil_run_diagnostics, tmp_path
    ):
        manifest = bounded_route_manifest(capacity=2).write(
            tmp_path / "overflow.json"
        ).path
        proc, report = run_diagnostics(sil_run_diagnostics, manifest, tmp_path)
        assert proc.returncode == 1
        publisher = task_rows(report)[("publisher", "tick")]
        assert publisher["failed"] == 1
        assert publisher["failed_activation"]["outcome"] == "failed"
        assert publisher["completed"] == publisher["attempted"] - 1
        assert validate_run_report(report) == []

    def test_response_deadline_failure_is_a_failed_step(
        self, sil_run_diagnostics, tmp_path
    ):
        proc, report = run_diagnostics(
            sil_run_diagnostics, wedged_manifest(tmp_path), tmp_path,
            "--participant-timeout-ms", "200",
        )
        assert proc.returncode == 1
        assert report["run"]["failure"]["category"] == "timeout"
        assert report["configuration"]["participant_timeout_ms"] == 200
        step = task_rows(report)[("wedged", "step")]
        assert (step["attempted"], step["failed"]) == (1, 1)
        assert validate_run_report(report) == []
        if RESOURCES_SUPPORTED:
            # The wedged child is killed and reaped, so it is still measured.
            _, _, processes = resource_records(report)
            assert processes["wedged"]["peak_rss_bytes"] > 0

    def test_interrupted_run_marks_the_interrupted_step(
        self, sil_run_diagnostics, tmp_path
    ):
        observer = tmp_path / "observer.txt"
        report_path = tmp_path / "run.json"
        runner = subprocess.Popen(
            [str(sil_run_diagnostics), str(wedged_manifest(tmp_path, observer)),
             "-o", str(tmp_path / "run.mcap"),
             "--diagnostics-out", str(report_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # The participant writes <observer>.step when the first Step arrives:
        # interrupt that Step, not the startup.
        step_marker = Path(f"{observer}.step")
        deadline = time.monotonic() + 10
        while not step_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert step_marker.exists(), "the first Step did not arrive"
        runner.send_signal(signal.SIGINT)
        _, stderr = runner.communicate(timeout=10)
        assert runner.returncode == 1
        assert "run interrupted" in stderr
        report = json.loads(report_path.read_text())
        assert report["run"]["status"] == "interrupted"
        step = task_rows(report)[("wedged", "step")]
        assert step["interrupted"] == 1
        assert step["failed_activation"]["outcome"] == "interrupted"
        assert validate_run_report(report) == []

    def test_manifest_error_reports_preflight_only(
        self, sil_run_diagnostics, tmp_path
    ):
        broken = tmp_path / "broken.json"
        broken.write_text("{}")
        proc, report = run_diagnostics(sil_run_diagnostics, broken, tmp_path)
        assert proc.returncode == 2
        assert report["run"]["status"] == "configuration_error"
        assert report["identity"]["manifest_hash"] is None
        phases = report["observational"]["phases"]
        assert type(phases["preflight_ns"]) is int
        for name in ("setup_ns", "scheduled_execution_ns", "shutdown_ns"):
            assert phases[name] is None
            assert phases["unavailable"][name] == "not_reached"
        assert report["execution"]["participants"] == []
        assert validate_run_report(report) == []


class TestReportPublication:
    def test_default_path_is_beside_the_recording(
        self, sil_run_diagnostics, tmp_path
    ):
        proc = subprocess.run(
            [str(sil_run_diagnostics), str(mixed_period_manifest(tmp_path)),
             "-o", str(tmp_path / "out.mcap")],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        report = tmp_path / "out.mcap.diagnostics.json"
        assert json.loads(report.read_text())["sil_run_diagnostics"] == 1
        # Nothing temporary is left beside it.
        assert sorted(p.name for p in tmp_path.glob("out.mcap*")) == [
            "out.mcap", "out.mcap.diagnostics.json",
            "out.mcap.provenance.json",
        ]

    def test_existing_report_is_rejected_before_any_participant_starts(
        self, sil_run_diagnostics, tmp_path
    ):
        report = tmp_path / "run.json"
        report.write_text("{\"earlier\": true}")
        proc, _ = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path,
            report=report,
        )
        assert proc.returncode == 2
        assert "already exists" in proc.stderr
        assert report.read_text() == "{\"earlier\": true}"
        assert not (tmp_path / "run.mcap").exists()

    def test_report_on_the_recording_path_is_rejected(
        self, sil_run_diagnostics, tmp_path
    ):
        proc, _ = run_diagnostics(
            sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path,
            "-o", str(tmp_path / "same.json"), report=tmp_path / "same.json",
        )
        assert proc.returncode == 2
        assert "another artifact" in proc.stderr
        assert not (tmp_path / "same.json").exists()

    @pytest.mark.parametrize("failing", [False, True])
    def test_unwritable_report_keeps_the_run_outcome(
        self, failing, sil_run, sil_run_diagnostics, tmp_path
    ):
        manifest = (failing_step_manifest(tmp_path) if failing
                    else mixed_period_manifest(tmp_path))
        ordinary = subprocess.run(
            [str(sil_run), str(manifest), "-o", str(tmp_path / "a.mcap")],
            capture_output=True, text=True,
        )
        proc, report = run_diagnostics(
            sil_run_diagnostics, manifest, tmp_path,
            "-o", str(tmp_path / "b.mcap"),
            report=tmp_path / "absent" / "run.json",
        )
        assert report is None
        assert proc.returncode == ordinary.returncode == (1 if failing else 0)
        # The report's own failure follows the Run's diagnostic.
        assert proc.stderr.startswith(ordinary.stderr.replace(
            "sil-run: ", "sil-run-diagnostics: "))
        assert "cannot write diagnostics report" in proc.stderr
        assert proc.stdout == ordinary.stdout


    def test_a_report_that_appears_during_the_run_is_never_replaced(
        self, sil_run_diagnostics, tmp_path
    ):
        """Preflight found no report; a file the Run creates later keeps its
        content, and the runner leaves no temporary file beside it."""
        reports = tmp_path / "reports"
        reports.mkdir()
        report = reports / "run.json"
        m = toy_manifest(duration_ns=20 * MS)
        m.add_channel("ticks", schema="toy.Counter")
        m.add_channel("echo", schema="toy.Counter")
        add_multitask(m)
        echo = ROOT / "tests" / "participants" / "echo.py"
        m.add_process(
            "pyecho",
            command=["/bin/sh", "-c",
                     f"printf earlier > '{report}'; "
                     f"exec '{sys.executable}' '{echo}'"],
            step_period_ns=10 * MS,
            subscribes=[SubscriberRoute("ticks",
                                        capacity=COMPAT_ROUTE_CAPACITY)],
            publishes=["echo"],
        )
        manifest = m.write(tmp_path / "late.json").path

        proc = subprocess.run(
            [str(sil_run_diagnostics), str(manifest), "--diagnostics-out",
             str(report), "-o", str(tmp_path / "run.mcap")],
            capture_output=True, text=True, cwd=tmp_path)

        assert proc.returncode == 0, proc.stderr
        assert "cannot write diagnostics report" in proc.stderr
        assert report.read_text() == "earlier"
        assert sorted(p.name for p in reports.iterdir()) == ["run.json"]


class TestOrdinaryRunnerIsUnchanged:
    def test_ordinary_runner_has_no_diagnostics_option(self, sil_run, tmp_path):
        proc = subprocess.run(
            [str(sil_run), str(mixed_period_manifest(tmp_path)),
             "--diagnostics-out", str(tmp_path / "run.json")],
            capture_output=True, text=True, cwd=tmp_path,
        )
        assert proc.returncode == 2
        assert "--diagnostics-out" not in proc.stderr
        assert not (tmp_path / "run.json").exists()

    def test_both_runners_report_the_same_version(
        self, sil_run, sil_run_diagnostics
    ):
        versions = [
            subprocess.run([str(r), "--version"], capture_output=True,
                           text=True).stdout
            for r in (sil_run, sil_run_diagnostics)
        ]
        assert versions[0] == versions[1] != ""


class TestClockShim:
    def test_diagnostics_use_host_time_and_participants_virtual_time(
        self, sil_run, sil_run_diagnostics, tmp_path
    ):
        m = toy_manifest(duration_ns=30 * MS)
        m.add_channel("readings", schema="toy.Counter")
        m.add_process(
            "vecu",
            command=[sys.executable,
                     str(ROOT / "tests" / "participants" / "clock_reader.py")],
            step_period_ns=10 * MS,
            publishes=["readings"],
            shim=True,
        )
        manifest = m.write(tmp_path / "shim.json").path
        ordinary = subprocess.run(
            [str(sil_run), str(manifest), "-o", str(tmp_path / "a.mcap")],
            capture_output=True, text=True,
        )
        started = time.monotonic_ns()
        proc, report = run_diagnostics(
            sil_run_diagnostics, manifest, tmp_path,
            "-o", str(tmp_path / "b.mcap"),
        )
        elapsed = time.monotonic_ns() - started
        assert ordinary.returncode == proc.returncode == 0, proc.stderr
        # The Participant still reads Virtual time, so the Recording is the
        # ordinary runner's, readings included.
        assert (tmp_path / "a.mcap").read_bytes() == (
            tmp_path / "b.mcap").read_bytes()
        # The collector reads the host clock: its Run wall time fits inside
        # the host time elapsed around the runner, and the Steps took real
        # time inside it.
        wall = report["observational"]["run_wall_time"]["wall_ns"]
        assert 0 < wall <= elapsed
        step = timing_rows(report)[("vecu", "step")]["timing"]
        assert step["completed"] == 3
        assert 0 < step["sum_ns"] <= wall


def member_paths(value, prefix=""):
    """Every member name in a document, with list positions collapsed."""
    paths = set()
    if isinstance(value, dict):
        for key, item in value.items():
            # `unavailable` maps are keyed by data, not by the schema.
            if key == "unavailable":
                paths.add(f"{prefix}.unavailable")
                continue
            paths.add(f"{prefix}.{key}")
            paths |= member_paths(item, f"{prefix}.{key}")
    elif isinstance(value, list):
        for item in value:
            paths |= member_paths(item, f"{prefix}[]")
    return paths


def test_live_report_has_the_frozen_example_shape(sil_run_diagnostics, tmp_path):
    """The runner and tests/fixtures/diagnostics/ cannot drift apart."""
    _, report = run_diagnostics(
        sil_run_diagnostics, mixed_period_manifest(tmp_path), tmp_path
    )
    frozen = json.loads(
        (ROOT / "tests" / "fixtures" / "diagnostics" / "run-success.json")
        .read_text()
    )
    assert member_paths(report) == member_paths(frozen)
