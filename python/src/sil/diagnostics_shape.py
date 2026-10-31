"""The member types of the diagnostics report contracts (#300, #304).

docs/diagnostics-report.md defines every member. This module states each one
a consumer reads with its JSON type, so a report that passes is one the HTML
report and the terminal summaries can present: every object member they read
is there, `_ns`, `_bytes` and count members are non-negative integers, means,
factors and rates are non-negative numbers, and a measured value may be null
only in an object with an `unavailable` map (diagnostics_report checks that
each such null names its reason). Members not stated here are allowed.
"""

from __future__ import annotations

from typing import Any, Callable

# A shape appends the violations of `value` at `where` to `errors`.
Shape = Callable[[Any, str, list], None]


def _leaf(test: Callable[[Any], bool], name: str) -> Shape:
    def check(value: Any, where: str, errors: list) -> None:
        if not test(value):
            errors.append(f"{where} is not {name}")
    return check


def _is_integer(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float)


COUNT = _leaf(lambda v: _is_integer(v) and v >= 0, "a non-negative integer")
INTEGER = _leaf(_is_integer, "an integer")
NUMBER = _leaf(lambda v: _is_number(v) and v >= 0, "a non-negative number")
TEXT = _leaf(lambda v: type(v) is str, "a string")
FLAG = _leaf(lambda v: type(v) is bool, "a boolean")


def one_of(values: set[str]) -> Shape:
    def check(value: Any, where: str, errors: list) -> None:
        if type(value) is not str or value not in values:
            errors.append(f"{where}: unknown value {value!r}")
    return check


def maybe(shape: Shape) -> Shape:
    """`shape`, or null."""
    def check(value: Any, where: str, errors: list) -> None:
        if value is not None:
            shape(value, where, errors)
    return check


def items(shape: Shape) -> Shape:
    """A list whose every item has `shape`."""
    def check(value: Any, where: str, errors: list) -> None:
        if type(value) is not list:
            errors.append(f"{where} is not a list")
            return
        for index, item in enumerate(value):
            shape(item, f"{where}[{index}]", errors)
    return check


def _member(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def members(required: dict[str, Shape],
            optional: dict[str, Shape] | None = None) -> Shape:
    """An object with every `required` member, and `optional` ones typed."""
    def check(value: Any, where: str, errors: list) -> None:
        if type(value) is not dict:
            errors.append(f"{where or 'the report'} is not an object")
            return
        for key, shape in required.items():
            if key not in value:
                errors.append(f"missing member '{_member(where, key)}'")
            else:
                shape(value[key], _member(where, key), errors)
        for key, shape in (optional or {}).items():
            if key in value:
                shape(value[key], _member(where, key), errors)
    return check


def reasoned(required: dict[str, Shape], measured: dict[str, Shape],
             optional: dict[str, Shape] | None = None) -> Shape:
    """An object with an `unavailable` map; each `measured` value can be
    null for a reason."""
    return members({**required,
                    **{key: maybe(shape) for key, shape in measured.items()},
                    "unavailable": members({})}, optional)


WORK_COUNTS = {key: COUNT for key in
               ("attempted", "completed", "failed", "interrupted")}
FAILED_ACTIVATION = maybe(members({"activation_index": COUNT,
                                   "virtual_time_ns": COUNT,
                                   "outcome": TEXT}))
TASK_ROW = members({"task": TEXT, "kind": TEXT, "period_ns": COUNT,
                    "offset_ns": COUNT, **WORK_COUNTS,
                    "failed_activation": FAILED_ACTIVATION})
REPLAY_ROW = members({"publications": COUNT, **WORK_COUNTS,
                      "failed_activation": FAILED_ACTIVATION})
EXECUTION = reasoned(
    {"participants": items(members({"participant": TEXT, "kind": TEXT,
                                    "tasks": items(TASK_ROW),
                                    "replay": maybe(REPLAY_ROW)}))},
    {"duration_ns": COUNT, "last_virtual_time_ns": COUNT})

TIMING = reasoned(
    {"scope": TEXT, "completed": COUNT, "sum_ns": COUNT,
     "quantiles": members({
         "method": TEXT, "approximate": FLAG, "sample_count": COUNT,
         "retained_count": COUNT,
         "reservoir": members({"algorithm": TEXT, "generator": TEXT,
                               "algorithm_version": COUNT, "capacity": COUNT,
                               "seed": COUNT})}),
     "samples_ns": items(COUNT),
     "slowest": items(members({"activation_index": COUNT,
                               "virtual_time_ns": COUNT, "wall_ns": COUNT}))},
    {"min_ns": COUNT, "max_ns": COUNT, "mean_ns": NUMBER, "stddev_ns": NUMBER,
     "p50_ns": COUNT, "p95_ns": COUNT, "p99_ns": COUNT})
FAILED_WALL = maybe(members({"activation_index": COUNT, "wall_ns": COUNT}))

RESOURCE_RECORD = {
    "required": {"scope": TEXT, "limitations": items(TEXT)},
    "measured": {"collector": TEXT, "interval": TEXT,
                 "includes_waited_descendants": FLAG, "user_cpu_ns": COUNT,
                 "system_cpu_ns": COUNT, "peak_rss_bytes": COUNT,
                 "peak_rss_kind": TEXT},
}
RESOURCES = members({
    "runner": reasoned(RESOURCE_RECORD["required"],
                       RESOURCE_RECORD["measured"]),
    "process_participants": items(reasoned(
        {**RESOURCE_RECORD["required"], "participant": TEXT},
        RESOURCE_RECORD["measured"])),
    "native_participants": TEXT,
    "cpu_total": reasoned({"scope": TEXT},
                          {"user_cpu_ns": COUNT, "system_cpu_ns": COUNT}),
})

PHASE_NAMES = ("preflight_ns", "setup_ns", "scheduled_execution_ns",
               "shutdown_ns", "recording_finalization_ns", "unattributed_ns")
OBSERVATIONAL = members({
    "run_wall_time": reasoned({"boundary": TEXT}, {"wall_ns": COUNT}),
    "phases": reasoned({}, {phase: COUNT for phase in PHASE_NAMES}),
    "tasks": items(members({"participant": TEXT, "task": TEXT, "kind": TEXT,
                            "timing": TIMING,
                            "failed_activation": FAILED_WALL})),
    "replay": items(members({"participant": TEXT, "timing": TIMING,
                             "failed_activation": FAILED_WALL})),
    "throughput": reasoned(
        {"denominator": TEXT, "completed_native_activations": COUNT,
         "completed_process_steps": COUNT},
        {"scheduled_execution_ns": COUNT, "native_activations_per_s": NUMBER,
         "process_steps_per_s": NUMBER}),
    "virtual_time_speed": reasoned(
        {}, {"scheduled_factor": NUMBER, "end_to_end_factor": NUMBER}),
    "resources": RESOURCES,
})

PROVENANCE_MODES = {"full", "external"}
SIZE_GUARDS = ("max_protocol_line_bytes", "max_step_output_messages",
               "max_step_inline_payload_bytes")
RUN_REPORT = members({
    "collector": members({"name": TEXT, "collector_version": COUNT,
                          "clock": TEXT, "slowest_retained": COUNT,
                          "p95_minimum_samples": COUNT,
                          "p99_minimum_samples": COUNT}),
    "identity": reasoned({}, {"manifest_path": TEXT, "manifest_hash": TEXT,
                              "sil_version": TEXT, "source_repository": TEXT,
                              "source_revision": TEXT}),
    "host": reasoned({}, {"os": TEXT, "os_release": TEXT,
                          "architecture": TEXT, "logical_cpu_count": COUNT,
                          "cpu_description": TEXT}),
    "configuration": members({"measurement_mode": TEXT, "recording": FLAG,
                              "participant_timeout_ms": maybe(COUNT),
                              **{guard: COUNT for guard in SIZE_GUARDS}},
                             {"provenance_mode": one_of(PROVENANCE_MODES)}),
    "run": members({"exit_code": INTEGER, "status": TEXT,
                    "failure": maybe(members({
                        "message": TEXT, "category": TEXT,
                        "participant": maybe(TEXT),
                        "virtual_time_ns": maybe(COUNT)}))}),
    "completeness": members({"run_complete": FLAG, "reasons": items(TEXT)}),
    "collection_errors": items(members({"scope": TEXT, "message": TEXT})),
    "execution": EXECUTION,
    "observational": OBSERVATIONAL,
    "artifacts": members({"recording": maybe(TEXT), "provenance": TEXT,
                          "failure_report": maybe(TEXT)}),
})

STATISTICS = ("min", "median", "max", "mean", "stddev")
WORK_TOTALS = ("requested", "attempted", "completed", "failed")
# The embedded Run reports are checked as Run reports, one by one.
BENCHMARK_REPORT = members({
    "generator": members({"name": TEXT, "sil_version": TEXT}),
    "measurement_mode": TEXT,
    "identity": reasoned({"manifest_path": TEXT},
                         {"manifest_hash": TEXT, "sil_version": TEXT,
                          "source_revision": TEXT,
                          "diagnostics_collector_version": COUNT}),
    "host": reasoned({}, {"os": TEXT, "os_release": TEXT,
                          "architecture": TEXT, "logical_cpu_count": COUNT,
                          "cpu_description": TEXT}),
    "policy": members({"repeats": COUNT, "warmup": COUNT, "recording": FLAG,
                       "execution": TEXT,
                       "participant_timeout_ms": maybe(COUNT),
                       **{guard: maybe(COUNT) for guard in SIZE_GUARDS}},
                      {"provenance_mode": one_of(PROVENANCE_MODES)}),
    "status": members({"complete": FLAG, "exit_code": INTEGER,
                       "stopped_reason": maybe(TEXT)}),
    "counts": members({prefix + key: COUNT for prefix in ("", "warmups_")
                       for key in WORK_TOTALS}),
    "runs": items(members({
        "label": TEXT, "role": TEXT, "index": COUNT, "directory": TEXT,
        "run_exit_code": maybe(INTEGER),
        "process_outcome": members({"exit_code": maybe(INTEGER),
                                    "signal": maybe(COUNT)}),
        "report_status": TEXT, "report_path": TEXT,
        "report": maybe(members({}))})),
    "aggregates": members({
        "scope": TEXT, "sample_count": COUNT,
        "metrics": items(reasoned(
            {"metric": TEXT, "unit": TEXT, "source": TEXT,
             "per_run": items(members({"label": TEXT,
                                       "value": maybe(NUMBER)}))},
            {statistic: NUMBER for statistic in STATISTICS},
            {"participant": TEXT}))}),
})


def shape_errors(shape: Shape, document: Any) -> list[str]:
    """Every member of `document` that does not have its stated type."""
    errors: list[str] = []
    shape(document, "", errors)
    return errors
