"""Verdicts the acceptance Run must reach, stated apart from executing it.

Every gate here raises; none returns a verdict a caller could ignore. They
stay importable without SiL, an FMU or a container so the same judgement is
tested directly.
"""
from __future__ import annotations

from acceptance_contract import FORBIDDEN_MODULES, FORBIDDEN_TOOLS
from proof_support import require
from sensitivity_compare import exceeded_fields


def envelope_verdict(row, comparison: dict, envelope: dict) -> dict:
    """Judge one measured row against the envelope authored before the Run."""
    exceeded = exceeded_fields(comparison, envelope)
    if row.negative_control:
        require(
            exceeded,
            f"{row.name}: the timing defect stayed inside the declared envelope",
        )
    else:
        require(
            not exceeded,
            f"{row.name}: sensitivity exceeded the declared envelope: {exceeded}",
        )
    return {
        "row": row.name,
        "negative_control": row.negative_control,
        "exceeded": exceeded,
        "within": not exceeded,
        "detected": bool(exceeded) if row.negative_control else False,
    }


def expected_failure_verdict(name: str, exit_code: int, errors: list[str]) -> dict:
    """A deliberate KPI failure is evidence only when it actually failed."""
    require(
        exit_code == 1,
        f"{name}: expected the KPI to fail the Run with exit 1, got exit {exit_code}"
        if exit_code
        else f"{name}: the Run succeeded although its KPI must fail it",
    )
    require(errors, f"{name}: failed without a post-hoc diagnostic naming the violation")
    return {"scenario": name, "expected_exit": 1, "diagnostic": errors[0]}


def require_minimal_runtime(present_modules, present_tools) -> dict:
    """The example image adds model runtime, never exporter or toolchain."""
    modules = sorted(set(present_modules) & set(FORBIDDEN_MODULES))
    require(
        not modules,
        f"the example image carries build or comparison modules: {modules}",
    )
    tools = sorted(set(present_tools) & set(FORBIDDEN_TOOLS))
    require(not tools, f"the example image carries build tools: {tools}")
    return {
        "absent_modules": list(FORBIDDEN_MODULES),
        "absent_tools": list(FORBIDDEN_TOOLS),
    }


def determinism_summary(results: dict) -> dict:
    """Per-Manifest determinism: one identity per check, repeats identical."""
    summary = {}
    for group in results.values():
        for name, result in group.items():
            identity = result.get("artifacts", result)
            summary[name] = {
                key: identity[key]
                for key in ("manifest_sha256", "authored_manifest_sha256",
                            "recording_sha256")
            }
    identities = {entry["manifest_sha256"] for entry in summary.values()}
    require(
        len(identities) == len(summary),
        "two acceptance checks share one Manifest identity",
    )
    for name, entry in summary.items():
        require(
            len(set(entry["authored_manifest_sha256"])) == 1,
            f"{name}: authoring the same Manifest twice produced different bytes",
        )
        require(
            len(set(entry["recording_sha256"])) == 1,
            f"{name}: two Runs of one Manifest produced different Recordings",
        )
    return summary
