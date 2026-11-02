"""Explicit repeated-experiment context for an offline profile comparison.

Benchmarks are independent experiments, not identity joins to a Profile. The
shared diagnostics validator checks their version, shape, finite values and
Run evidence; this reader also verifies campaign and aggregate consistency.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
from dataclasses import dataclass
from pathlib import Path

from sil.diagnostics_format import provenance_mode
from sil.diagnostics_html_markup import Html, section, table, tag
from sil.diagnostics_report import BENCHMARK_REPORT_KEY, parse_report
from sil.profile_set import MAX_INPUT_BYTES, ProfileSet, ProfileSetError

ROW_LIMIT = 100
TASK_ROW_LIMIT = 500
CELL_CHAR_LIMIT = 4096
NOTICE = ("Separate repeated experiments: benchmarks are not the same Runs as "
          "the profiles. Successful measured Runs only enter aggregates; "
          "warm-ups excluded. Per-Run Task percentiles retain their own "
          "reservoir and measurement scope; never pooled or averaged. "
          "Matching Manifests do not establish the same binaries. No "
          "significance verdict, collector-overhead subtraction or performance gate.")


@dataclass
class Benchmark:
    path: str
    sha256: str
    bytes: int
    report: dict


def load_benchmark(path: Path) -> Benchmark:
    """Load exactly the explicitly requested file, subject to the profile cap."""
    try:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_INPUT_BYTES:
                raise ProfileSetError(f"benchmark '{path}' exceeds {MAX_INPUT_BYTES} bytes")
            data = stream.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ProfileSetError(f"benchmark '{path}' exceeds {MAX_INPUT_BYTES} bytes")
        report = parse_report(data.decode("utf-8"), source=str(path))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ProfileSetError(f"invalid benchmark '{path}': {error}") from error
    if BENCHMARK_REPORT_KEY not in report:
        raise ProfileSetError(f"'{path}' is not a benchmark report")
    _check_consistency(report)
    return Benchmark(str(path), hashlib.sha256(data).hexdigest(), len(data), report)


def _successful(run: dict) -> bool:
    return run["run_exit_code"] == 0 and run["report"] is not None


def _metric_value(metric: dict, report: dict):
    source = report
    if "participant" in metric:
        source = next((r for r in report["observational"]["resources"]["process_participants"]
                       if r["participant"] == metric["participant"]), {})
        return source.get(metric["source"].rsplit(".", 1)[-1])
    try:
        for member in metric["source"].split("."):
            source = source[member]
    except (KeyError, TypeError):
        raise ProfileSetError(f"benchmark aggregate has unknown source '{metric['source']}'") from None
    return source


def _check_consistency(report: dict) -> None:
    def require(condition, reason):
        if not condition:
            raise ProfileSetError("inconsistent benchmark: " + reason)

    runs, counts, policy = report["runs"], report["counts"], report["policy"]
    labels = [run["label"] for run in runs]
    require(len(labels) == len(set(labels)), "duplicate Run labels")
    for role, prefix, requested in (("measured", "", "repeats"),
                                     ("warmup", "warmups_", "warmup")):
        group = [r for r in runs if r["role"] == role]
        completed = sum(_successful(r) for r in group)
        require(counts[prefix + "requested"] == policy[requested], "policy/count mismatch")
        require(counts[prefix + "completed"] == completed, "completed count differs from Runs")
        require(counts[prefix + "failed"] == len(group) - completed, "failed count differs from Runs")
        require(len(group) <= counts[prefix + "requested"], "more attempted than requested Runs")
    complete = all(counts[p + "completed"] == counts[p + "requested"]
                   for p in ("", "warmups_"))
    require(report["status"]["complete"] == complete, "completion status differs from counts")
    require(report["aggregates"]["scope"] == "successful_measured_runs", "unsupported aggregate scope")
    successful = [r for r in runs if r["role"] == "measured" and _successful(r)]
    seen = set()
    for metric in report["aggregates"]["metrics"]:
        key = (metric["metric"], metric.get("participant"))
        require(key not in seen, "duplicate aggregate metric")
        seen.add(key)
        expected = [{"label": r["label"], "value": _metric_value(metric, r["report"])}
                    for r in successful]
        require(metric["per_run"] == expected, "aggregate values differ from measured Run evidence")
        values = [row["value"] for row in expected]
        if not values or None in values:
            require(all(metric[k] is None for k in ("min", "median", "max", "mean", "stddev")),
                    "aggregate statistics present with unavailable observations")
        else:
            stats = {"min": min(values), "median": statistics.median(values),
                     "max": max(values), "mean": statistics.fmean(values),
                     "stddev": statistics.pstdev(values)}
            require(all(metric[k] is not None and math.isclose(metric[k], v, rel_tol=1e-12,
                                                              abs_tol=1e-9)
                        for k, v in stats.items()), "aggregate statistics differ from measured Runs")


def _display(text: str) -> str:
    if len(text) > CELL_CHAR_LIMIT:
        return (text[:CELL_CHAR_LIMIT] +
                f" … [{len(text) - CELL_CHAR_LIMIT} characters omitted; see Download JSON]")
    return text


def _json(value) -> str:
    return _display(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _identities(benchmark: Benchmark) -> dict:
    r = benchmark.report
    return {"Identity": r["identity"], "Host": r["host"], "Policy": r["policy"],
            "Provenance mode": provenance_mode(r["policy"]),
            "Measurement mode": r["measurement_mode"], "Counts": r["counts"]}


def benchmark_context(base: Benchmark, new: Benchmark,
                      profiles: tuple[ProfileSet, ProfileSet]) -> tuple[list[str], Html]:
    """A bounded static view with each side and each Run explicitly named."""
    text, content = [NOTICE], [tag("p", NOTICE)]
    before, after = _identities(base), _identities(new)
    changed = [key for key in before if before[key] != after[key]]
    warning = ("WARNING: benchmark experiments differ: " + ", ".join(changed)
               if changed else "Benchmark metadata matches; unknown identities remain unknown.")
    text.append(warning)
    content.append(tag("p", warning, class_="callout tone-warn" if changed else "note"))
    for label, benchmark, profile in zip(("BASE", "NEW"), (base, new), profiles):
        r = benchmark.report
        metadata = {"Input path": benchmark.path, "SHA-256": benchmark.sha256,
                    "Bytes": benchmark.bytes, "Schema": r[BENCHMARK_REPORT_KEY],
                    **_identities(benchmark), "Status": r["status"]}
        notices = []
        for key in ("manifest_hash", "sil_version", "source_revision"):
            value = r["identity"][key]
            if value is None or profile.report["identity"][key] is None:
                notices.append(f"{key}: unknown profile/benchmark identity")
            elif value != profile.report["identity"][key]:
                notices.append(f"{key}: profile/benchmark mismatch")
        # The profile and diagnostics contracts name the kernel differently
        # and retain different host details. Compare only shared evidence.
        host_pairs = [(profile.report["host"].get(profile_key), r["host"].get(benchmark_key))
                      for profile_key, benchmark_key in
                      (("os", "os"), ("architecture", "architecture"),
                       ("kernel_release", "os_release"))]
        if any(before is not None and after is not None and before != after
               for before, after in host_pairs):
            notices.append("Host: profile/benchmark mismatch")
        if any(before is None or after is None for before, after in host_pairs):
            notices.append("Host: unknown common profile/benchmark identity")
        if provenance_mode(r["policy"]) != provenance_mode(profile.report["configuration"]):
            notices.append("Provenance mode: profile/benchmark mismatch")
        for key, value in r["policy"].items():
            configuration = profile.report["configuration"]
            if key == "provenance_mode" or key not in configuration:
                continue
            # A profile keeps the Recording path and requested guard values;
            # diagnostics keeps Recording presence and effective defaults.
            requested = (configuration[key] is not None if key == "recording"
                         else configuration[key])
            if requested is not None and requested != value:
                notices.append(f"{key}: profile/benchmark configuration mismatch")
        if r["measurement_mode"] != "diagnostics":
            notices.append("Measurement mode differs from the diagnostics profile Run")
        differences = set()
        for run in r["runs"]:
            report = run["report"]
            if report is None:
                continue
            if any(report["identity"][key] != r["identity"][key]
                   for key in ("manifest_hash", "sil_version", "source_revision")):
                differences.add("identity")
            if report["host"] != r["host"]:
                differences.add("host")
            if provenance_mode(report["configuration"]) != provenance_mode(r["policy"]):
                differences.add("provenance mode")
            if any(report["configuration"][key] != value for key, value in r["policy"].items()
                   if key != "provenance_mode" and key in report["configuration"]):
                differences.add("configuration")
        if differences:
            notices.append("Within-experiment metadata differs: " + ", ".join(sorted(differences)))
        metadata["Profile comparison"] = notices or "Common metadata matches; binary identity is unavailable."
        meta_rows = [[key, _json(value)] for key, value in metadata.items()]
        text += [f"{label} benchmark", *(f"{key}: {value}" for key, value in meta_rows)]
        if notices:
            text.append("WARNING: " + "; ".join(notices))
        rows = [[_display(m["metric"]), _display(m.get("participant", "all")), _display(m["unit"]),
                 *(_json(m[k]) for k in ("min", "median", "max", "mean", "stddev")),
                 _json(m["unavailable"])] for m in r["aggregates"]["metrics"][:ROW_LIMIT]]
        metrics = table(f"{label}-benchmark-metrics", "Repeated Run/phase distributions",
                        [(key, False) for key in ("Metric", "Participant", "Unit", "min",
                         "median", "max", "mean", "stddev", "Unavailable reasons")], rows)
        runs, tasks = [], []
        task_count = 0
        for run in r["runs"]:
            report = run["report"]
            if len(runs) < ROW_LIMIT:
                runs.append([_display(run["label"]), run["role"], _json(run["run_exit_code"]),
                             _json(run["process_outcome"]),
                             _json({key: report[key] for key in ("identity", "host", "configuration")})
                             if report else "unavailable: diagnostics report missing",
                             _json(report["artifacts"]) if report else "unavailable"])
            if report is None or run["role"] != "measured":
                continue
            for task in report["observational"]["tasks"]:
                task_count += 1
                if len(tasks) < TASK_ROW_LIMIT:
                    timing = {key: value for key, value in task["timing"].items()
                              if key not in ("samples_ns", "slowest")}
                    tasks.append([_display(run["label"]), run["report"]["run"]["status"],
                                  _display(task["participant"]), _display(task["task"]),
                                  _json(timing), _json(task["failed_activation"])])
        omitted = (f"Not displayed: {max(0, len(r['aggregates']['metrics']) - ROW_LIMIT)} "
                   f"aggregate metrics, {max(0, len(r['runs']) - ROW_LIMIT)} Run metadata rows, "
                   f"{task_count - len(tasks)} per-Run Task rows. Full input reports are in Download JSON.")
        text += ["Repeated Run/phase distributions", *(" | ".join(row) for row in rows),
                 "Run identities and outcomes", *(" | ".join(row) for row in runs),
                 "Per-Run Task observations (ns)", *(" | ".join(row) for row in tasks), omitted]
        content.append(section(f"{label}-benchmark", f"{label} benchmark",
            table(f"{label}-benchmark-identity", "Experiment identity", [("Member", False), ("Value", False)], meta_rows),
            tag("p", "; ".join(notices) if notices else "Binary identity unavailable; matching metadata is not proof of identical binaries."),
            metrics, table(f"{label}-benchmark-runs", "Run identities and outcomes",
                [(key, False) for key in ("Run", "Role", "Exit code", "Process outcome", "Identity / host / configuration", "Artifacts (reported paths; immutable binary identities unavailable)")], runs),
            tag("p", "Failed/partial Runs remain separate. Each Task row uses only that Run's completed Activations, with null reasons and reservoir scope retained."),
            table(f"{label}-benchmark-tasks", "Per-Run Task observations (ns)",
                [(key, False) for key in ("Run", "Status", "Participant", "Task", "Timing and quantile scope", "Failed Activation")], tasks),
            tag("p", omitted)))
    return text, section("benchmarks", "Repeated benchmark context", *content)
