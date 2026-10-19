"""Parts of the profile HTML report (#318) that both collectors use.

The status banner, the identity and method facts, the labels of reasons and
limitations, value formatting with explicit denominators, source-line cells
and the caller tree. Every value comes from the saved set; markup comes from
sil.diagnostics_html_markup, which escapes all report text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable, Sequence

from sil.diagnostics_html_markup import (
    ICONS, Html, code, definition_list, join, plain, section, tag,
)

RUN_LABELS = {
    "succeeded": ("Run succeeded", "ok"),
    "run_failure": ("Run failed", "fail"),
    "configuration_error": ("Run configuration error", "fail"),
    "interrupted": ("Run interrupted", "warn"),
    "ended_by_signal": ("Runner ended by a signal", "fail"),
    "not_started": ("Run not started", "fail"),
}
CAPTURE_LABELS = {
    "complete": ("Capture complete", "ok"),
    "partial": ("Capture partial", "warn"),
    "interrupted": ("Capture interrupted", "warn"),
    "failed": ("Capture failed", "fail"),
    "not_started": ("Capture not started", "fail"),
    "unavailable": ("Capture unavailable", "warn"),
}
CORRELATION_LABELS = {
    "complete": ("Activation correlation complete", "ok"),
    "partial": ("Activation correlation partial", "warn"),
    "unavailable": ("Activation correlation unavailable", "warn"),
    "failed": ("Activation correlation failed", "fail"),
}

# docs/profile-report.md: the reasons of v1. An unknown reason shows as is.
REASONS = {
    "run_not_started": "The Run did not start, so there is nothing to capture",
    "no_diagnostics_report": "The runner wrote no valid diagnostics report",
    "not_reported_by_backend": "The backend did not report this value",
    "no_build_id": "The module has no recorded build ID",
    "module_unavailable": "No file with the recorded build ID was available",
    "debug_file_mismatched":
        "A debug file with another build ID was found; it is not used",
    "debug_file_not_found": "No debug file with the module's build ID",
    "unverifiable_debug_file":
        "The module has no build ID, so no debug file can be matched",
    "symbolization_timeout": "A perf script pass took longer than its limit",
    "export_failed": "An export failed or its output did not decode",
    "no_samples": "The capture has no sample to describe",
    "not_requested": "Not requested",
    "reserved": "Reserved for a later version; not measured",
    "lost_samples": "perf reports lost samples: the totals miss them",
    "capture_limit_reached":
        "The capture reached its size limit; later data is not in it",
    "interrupted": "The command was interrupted",
    "backend_failed": "The backend failed",
    "data_truncated": "The last record of the data is cut",
    "data_may_be_incomplete":
        "The traced process did not end normally, so its last data can be "
        "missing",
    "no_data": "The backend wrote no data: the scope process did not start",
    "unsupported_scope": "This version cannot capture the requested scope",
    "observations_missing": "The runner wrote no observation stream",
    "observations_corrupt": "The observation stream does not decode",
    "observations_truncated":
        "The storage limit stopped the observation stream; later "
        "Activations are missing",
    "observations_write_failed":
        "A write to the observation stream failed; later records are missing",
    "observations_dropped": "An observation record could not be made",
    "observations_unterminated":
        "The observation stream has no end: the runner did not end normally",
    "clock_mismatch": "The two clocks are not proven to be the same",
    "runner_not_observed": "The capture has no runner process of the stream",
    "pid_namespace_mismatch": "The runner and the command are in different "
                              "PID namespaces",
    "pid_namespace_unverified": "The PID namespace cannot be verified",
    "no_samples_file": "The samples file was not written",
    "process_unmapped": "A Process participant has no unique perf process",
    "no_validated_clock_mapping":
        "Heaptrack's timestamps have no validated mapping to host time",
    "process_outside_step":
        "A Process participant's process between its Steps",
    "runner_other_thread": "A runner thread that runs no Activation",
    "runner_outside_intervals":
        "The runner thread before the first or after the last phase",
    "unmapped_process":
        "A runner descendant outside every mapped participant's tree",
    "launcher": "The launcher process",
    "observations_incomplete":
        "At or after the last kept record of a cut observation stream",
}

LIMITATIONS = {
    "cpu_samples_estimate_on_cpu_time":
        "Sample weights estimate on-CPU time. They are not exact function "
        "durations or call counts, and they do not explain wall time: "
        "waiting, IPC and scheduler delay have no samples.",
    "user_space_only": "Only user-space code is sampled (:u); kernel code "
                       "is not.",
    "scope_is_runner_and_descendants":
        "Only the runner and its descendants are sampled.",
    "host_build_only":
        "The profile describes this host build. It is not a target-ECU "
        "measurement or a worst-case execution time.",
    "heap_tracks_allocation_calls_only":
        "Only the interposed allocation functions are seen: custom pools, "
        "mmap and stack memory are not.",
    "outstanding_is_not_a_leak_verdict":
        "Allocations outstanding at capture end are not proven leaks.",
    "allocation_origin_is_not_ownership":
        "An allocation site is where memory was allocated, not its owner.",
    "activation_correlation_unavailable":
        "No allocation is related to an Activation or a Step.",
}


def share(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


def reason_text(reason: str) -> Html:
    return tag("span", tag("code", reason), " ",
               REASONS.get(reason, ""), class_="reason-line")


def reasons_list(reasons: Sequence[str]) -> Html | None:
    if not reasons:
        return None
    return tag("ul", *(tag("li", reason_text(r)) for r in reasons),
               class_="limitations")


def status_line(label: str, tone: str, detail: Any = None,
                reasons: Sequence[str] = ()) -> Html:
    return tag("li",
               tag("span", ICONS[tone], class_="icon", aria_hidden="true"),
               " ", tag("strong", label),
               join([" · ", detail]) if detail else None,
               reasons_list(reasons), class_=f"status-line tone-{tone}")


def _worst(tones: Iterable[str]) -> str:
    tones = list(tones)
    return "fail" if "fail" in tones else "warn" if "warn" in tones else "ok"


def run_detail(run: dict) -> str:
    if run["exit_code"] is not None:
        return f"exit code {run['exit_code']}"
    if run["signal"] is not None:
        return f"signal {run['signal']}"
    return "no exit code"


def banner(report: dict, kind_label: str,
           correlation: tuple[str, str, Any, Sequence[str]] | None) -> Html:
    """The independent Run, capture and correlation statuses, first."""
    run, capture = report["run"], report["capture"]
    run_label, run_tone = RUN_LABELS.get(run["status"], (run["status"],
                                                         "warn"))
    capture_label, capture_tone = CAPTURE_LABELS.get(
        capture["status"], (capture["status"], "warn"))
    lines = [status_line(run_label, run_tone, run_detail(run)),
             status_line(capture_label, capture_tone,
                         reasons=capture["reasons"])]
    tones = [run_tone, capture_tone]
    if correlation is not None:
        label, tone, detail, reasons = correlation
        lines.append(status_line(label, tone, detail, reasons))
        tones.append(tone)
    failure = run["failure"]
    tone = _worst(tones)
    return tag(
        "section",
        tag("p", kind_label, class_="eyebrow"),
        tag("h1", tag("span", ICONS[tone], class_="icon", aria_hidden="true"),
            " ", f"{run_label} · {capture_label}", class_="status-title"),
        tag("p", report["identity"]["manifest_path"], class_="subtitle"),
        tag("ul", *lines, class_="status-lines"),
        tag("p", tag("strong", "First failure: "),
            tag("span", failure["message"], class_="message"),
            f" ({failure['category']})", class_="banner-detail")
        if failure else None,
        tag("p", "The Run outcome and the capture outcome are separate: a "
                 "capture problem never replaces the Run's exit code.",
            class_="banner-detail"),
        class_=f"banner tone-{tone}", aria_label="Status")


def value_or_reason(record: dict, key: str) -> Html:
    if record.get(key) is not None:
        return code(record[key])
    reason = record.get("unavailable", {}).get(key)
    return tag("span", "unknown", join([" (", tag("code", reason), ")"])
               if reason else None, class_="na")


def identity_section(report: dict, method: Sequence[tuple[str, Any]]) -> Html:
    identity, host = report["identity"], report["host"]
    configuration = report["configuration"]
    guards = [(name, configuration[key]) for name, key in (
        ("Participant timeout (ms)", "participant_timeout_ms"),
        ("Max protocol line (bytes)", "max_protocol_line_bytes"),
        ("Max Step output Messages", "max_step_output_messages"),
        ("Max Step inline payload (bytes)", "max_step_inline_payload_bytes"))]
    return section(
        "measured", "What was measured",
        tag("div",
            tag("div", tag("h3", "Run and build"), definition_list([
                ("Manifest", code(identity["manifest_path"])),
                ("Manifest hash", value_or_reason(identity, "manifest_hash")),
                ("SiL version", value_or_reason(identity, "sil_version")),
                ("Source revision",
                 value_or_reason(identity, "source_revision")),
                ("Source repository",
                 value_or_reason(identity, "source_repository")),
                ("Recording", plain(configuration["recording"],
                                    "none (--no-recording)")),
                ("Working directory", code(configuration["cwd"])),
                *((name, plain(value, "not set")) for name, value in guards),
                ("Producer", f"{report['producer']['name']} "
                             f"{report['producer']['sil_version']}"),
            ])),
            tag("div", tag("h3", "Host"), definition_list([
                ("OS", host["os"]), ("Kernel", code(host["kernel_release"])),
                ("Architecture", host["architecture"]),
                ("perf_event_paranoid",
                 value_or_reason(host, "perf_event_paranoid")),
            ]), tag("h3", "Method"), definition_list(method)),
            class_="grid-2"))


def limitations_section(report: dict) -> Html:
    return section(
        "limitations", "Limitations",
        tag("ul", *(tag("li", LIMITATIONS.get(name, ""), " ",
                        tag("code", name, class_="muted"))
                    for name in report["limitations"]),
            class_="limitations"),
        note="These statements apply to every value of this report.")


def source_cell(lines: Sequence[dict], count_key: str) -> Html:
    """Source lines as text; an absolute path also links as a file: URL.

    The href is built from the path, never taken from the report, so no
    report value can become a javascript: or any other URL."""
    parts = []
    for line in lines:
        label = f"{line['file']}:{line['line']}"
        path = PurePosixPath(line["file"])
        text = (tag("a", label, href=path.as_uri(), class_="source")
                if path.is_absolute() else tag("code", label))
        parts.append(tag("li", text, f" ({line[count_key]})"))
    return tag("ul", *parts, class_="source-lines") if parts else \
        tag("span", "none", class_="na")


def function_cell(symbol: str | None, module_path: str | None) -> Html:
    name = (tag("span", symbol, class_="name fn") if symbol is not None
            else tag("span", "[unknown function]", class_="na"))
    module = (tag("code", PurePosixPath(module_path).name, title=module_path,
                  class_="module")
              if module_path is not None else
              tag("span", "[unknown module]", class_="na"))
    return join([name, Html("<br>"), module])


# --- caller tree --------------------------------------------------------


@dataclass(frozen=True)
class TreeLimits:
    """Bounds of a rendered tree. Each hidden part is a visible remainder."""

    min_share: float = 0.005    # of the tree's weight
    max_children: int = 20
    max_depth: int = 64
    max_nodes: int = 1500


@dataclass
class _Node:
    count: int = 0
    weight: int = 0
    self_count: int = 0
    self_weight: int = 0
    children: dict = field(default_factory=dict)


def build_tree(paths: Iterable[tuple[Sequence[int], int, int]]) -> _Node:
    """A top-down tree of (function keys outermost first, count, weight).

    A path adds to each node on it once, so a recursive function is a
    deeper node, and no count is added twice to one node."""
    root = _Node()
    for keys, count, weight in paths:
        node = root
        node.count += count
        node.weight += weight
        for key in keys:
            node = node.children.setdefault(key, _Node())
            node.count += count
            node.weight += weight
        node.self_count += count
        node.self_weight += weight
    return root


def render_tree(root: _Node, label: Callable[[int], Html],
                value: Callable[[int], str], unit: str,
                limits: TreeLimits = TreeLimits()) -> Html:
    """Nested <details>: Tab reaches each node, Enter opens it."""
    budget = [limits.max_nodes]
    floor = max(1, root.weight * limits.min_share)

    def numbers(node: _Node) -> Html:
        return tag("span",
                   tag("span", value(node.weight), class_="num"),
                   f" {share(node.weight, root.weight)}",
                   f" · {node.count} {unit}",
                   (f" · self {value(node.self_weight)}"
                    if node.self_count else None),
                   class_="tree-values")

    def other(text: str, count: int, weight: int) -> Html:
        return tag("li", tag("span", text, class_="na"), " ",
                   tag("span", value(weight), class_="num"),
                   f" {share(weight, root.weight)} · {count} {unit}",
                   class_="tree-other")

    def children(node: _Node, depth: int) -> list[Html]:
        ordered = sorted(node.children.items(),
                         key=lambda item: (-item[1].weight, item[0]))
        shown, hidden = [], []
        for key, child in ordered:
            if (len(shown) < limits.max_children and child.weight >= floor
                    and depth < limits.max_depth and budget[0] > 0):
                budget[0] -= 1
                shown.append((key, child))
            else:
                hidden.append(child)
        items = [item(key, child, depth + 1) for key, child in shown]
        if hidden:
            text = (f"{len(hidden)} deeper callees not shown"
                    if depth >= limits.max_depth else
                    f"{len(hidden)} other callees")
            items.append(other(text, sum(c.count for c in hidden),
                               sum(c.weight for c in hidden)))
        return items

    def item(key: int, node: _Node, depth: int) -> Html:
        head = join([label(key), " ", numbers(node)])
        below = children(node, depth)
        if not below:
            return tag("li", tag("div", head, class_="tree-leaf"))
        return tag("li", tag("details", tag("summary", head),
                             tag("ul", *below), open=depth <= 2))

    if root.count == 0:
        return tag("p", "No samples: the tree is empty. That is not "
                        "evidence of no use.", class_="note")
    return tag("ul", tag("li", tag("details", tag("summary", join([
        tag("strong", "All"), " ", numbers(root)])), tag("ul", *children(
            root, 0)), open=True)), class_="tree", role="list")
