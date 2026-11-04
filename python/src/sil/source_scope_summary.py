"""The terminal view of a saved source-scope capture (#373).

docs/proposals/native-source-scopes.md, "Presentation": statuses,
identities, loss, per-Task exact and unavailable counts, the per-scope count
table and the fixed limitations. Every report text goes through `_clean`, so
no name can act on the terminal.
"""

from __future__ import annotations

from pathlib import Path

from sil.profile_summary import _clean
from sil.source_scope_report import LIMITATIONS
from sil.source_scope_set import ScopeSet


def format_scope_summary(scope_set: ScopeSet, path: Path) -> str:
    report = scope_set.report
    lines = [f"scopes: {_clean(path)}", _run_line(report["run"]),
             _capture_line(report["capture"]), _loss_line(report["capture"]),
             *_identity_lines(report["identities"]),
             *_task_lines(report["summary"]["tasks"]),
             *_scope_lines(report["summary"]["scopes"]),
             "limitations:",
             *(f"  - {item}" for item in LIMITATIONS)]
    return "\n".join(lines) + "\n"


def _run_line(run: dict) -> str:
    exit_text = (f"exit code {run['exit_code']}" if run["exit_code"]
                 is not None else f"signal {run['signal']}"
                 if run["signal"] else "no exit code")
    return f"run: {_clean(run['status'])}, {exit_text}"


def _capture_line(capture: dict) -> str:
    reasons = ", ".join(_clean(r) for r in capture["reasons"])
    return (f"capture: {_clean(capture['status'])}"
            + (f" ({reasons})" if reasons else "")
            + f", capture ID {_clean(capture['capture_id'] or 'unknown')}"
            + f", {_known(capture['records'])} records kept")


def _loss_line(capture: dict) -> str:
    loss = capture["loss"]
    if loss is None:
        return "loss: unknown (the raw header could not be read)"
    return ("loss: " + ", ".join(f"{key} {loss[key]}" for key in
                                 ("dropped_full", "dropped_contended",
                                  "clock_errors")))


def _known(value) -> str:
    return "unknown" if value is None else _clean(value)


def _library(identity: dict | None) -> str:
    if identity is None:
        return "not recorded"
    return (f"{_clean(identity['path'])} (build ID "
            f"{_known(identity['build_id'])}, SHA-256 "
            f"{_known(identity['sha256'])})")


def _identity_lines(identities: dict) -> list[str]:
    return [f"instrumented build: {_library(identities.get('instrumented'))}",
            f"original build: {_library(identities.get('original'))}",
            f"observer version {_known(identities.get('observer_version'))}, "
            f"compiler {_known(identities.get('compiler'))}"]


def _task_lines(tasks: list) -> list[str]:
    lines = ["tasks:"]
    for task in tasks:
        unavailable = task["unavailable"]
        reasons = ", ".join(f"{_clean(r)} {n}" for r, n in unavailable.items())
        lines.append(
            f"  {_clean(task['participant'])}/{_clean(task['task'])}: "
            f"{task['activations']} Activations, {task['exact']} exact, "
            f"{sum(unavailable.values())} unavailable"
            + (f" ({reasons})" if reasons else ""))
    if not tasks:
        lines.append("  no Activation (no decoded calls)")
    return lines


def _scope_lines(scopes: list) -> list[str]:
    lines = ["scopes (calls per exact Activation: min / median / max):"]
    for scope in scopes:
        per = scope["per_exact_activation"]
        lines.append(
            f"  {scope['id']:>5}  {_clean(scope['name'])}: "
            f"{scope['attributed_calls']} calls; "
            + " / ".join(_known(per[k]) for k in ("min", "median", "max")))
    if not scopes:
        lines.append("  no scope count (no decoded calls)")
    return lines
