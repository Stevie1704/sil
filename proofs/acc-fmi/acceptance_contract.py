"""Authored contract for the ACC consumer acceptance bundle (#151).

Fixed before a bundle is prepared or executed: which checks run from the
installed SiL bundle, which pinned artifacts they consume, the FMI 3.0 profile
this evidence establishes, and the baseline signals and KPIs a later
CAN-connected ADAS regression can reuse.
"""
from __future__ import annotations

from pathlib import Path

from proof_support import require
from scenario_contract import FIELDS as SCENARIO_FIELDS
from scenario_contract import configuration as scenario_configuration
from sensitivity_contract import (
    ACCEPTANCE_ENVELOPE,
    CHANNEL_FIELDS,
    MANEUVER_PERIOD_NS,
    MIN_GAP_M,
    REFERENCE_ABS_TOL,
    SensitivityRow,
    measured_rows,
    reference_rows,
)

BUNDLE_FORMAT = 1
MODELS = ("AccController", "AccPlant")
# Both images resolve the pinned archives here, so a Manifest authored during
# preparation and one authored inside the example image name the same files.
FMU_DIR = Path("/fmus")
BUNDLE_DIR = Path("/bundle")
INDEX_NAME = "bundle.json"
IMAGES_NAME = "images.json"
REFERENCE_DIR = "references"

# The nominal comparison and the deliberate KPI failure. Both also supply the
# per-Manifest determinism evidence: each is authored twice and run twice.
SCENARIO_CHECKS = ("nominal", "dropped")
# The acceptance envelope and its negative control. The full matrix stays in
# the sensitivity proof; the bundle re-establishes the verdict from the
# installed runtime.
SENSITIVITY_CHECKS = ("latency-20ms", "timing-defect")

# An installed bundle carries no exporter, no independent importer and no
# toolchain. The example image is the production runtime plus this directory.
FORBIDDEN_MODULES = ("fmpy", "pythonfmu3", "pytest")
FORBIDDEN_TOOLS = ("cc", "gcc", "c++", "g++", "cmake", "make", "git")

# Declared by the qualified archives. A changed attribute is a changed model,
# not a new capability of this bundle.
DECLARED_CO_SIMULATION = {
    "needsExecutionTool": "true",
    "canBeInstantiatedOnlyOncePerProcess": "false",
    "canGetAndSetFMUState": "false",
    "canSerializeFMUState": "false",
    "canHandleVariableCommunicationStepSize": "false",
}
EXERCISED_SYMBOLS = (
    "fmi3InstantiateCoSimulation",
    "fmi3EnterInitializationMode",
    "fmi3ExitInitializationMode",
    "fmi3SetFloat64",
    "fmi3GetFloat64",
    "fmi3DoStep",
    "fmi3Terminate",
    "fmi3FreeInstance",
)


def sensitivity_row(name: str) -> SensitivityRow:
    for row in measured_rows() + reference_rows():
        if row.name == name:
            return row
    raise KeyError(name)


def reference_names() -> tuple[str, ...]:
    """Independent trajectories the bundle pins, in preparation order.

    Each measured row is compared against its own independent trajectory, the
    finer trajectory it refines toward, and the 10 ms comparison that carries
    the envelope verdict.
    """
    names: list[str] = []
    for check in SENSITIVITY_CHECKS:
        row = sensitivity_row(check)
        for name in (row.name, row.reference, row.sensitivity_reference):
            if name is not None and name not in names:
                names.append(name)
    return tuple(names)


def scenario_checks() -> dict[str, dict]:
    return {name: scenario_configuration(name) for name in SCENARIO_CHECKS}


def fmi_profile(archives: dict) -> dict:
    """Summarize the profile this evidence establishes, not FMI conformance."""
    for model in MODELS:
        require(model in archives, f"{model}: missing archive audit")
        declared = archives[model]["capabilities"]
        for attribute, value in DECLARED_CO_SIMULATION.items():
            require(
                declared.get(attribute) == value,
                f"{model}: CoSimulation {attribute}={declared.get(attribute)!r} "
                f"contradicts the authored profile value {value!r}",
            )
        symbols = archives[model]["exported_symbols"]
        for symbol in EXERCISED_SYMBOLS:
            require(symbol in symbols, f"{model}: missing exercised symbol {symbol}")
    return {
        "standard": "FMI 3.0",
        "interface_type": "Co-Simulation",
        "scope": (
            "The profile these two source-available ACC FMUs and this bundle's "
            "Runs establish on Linux x86-64. It is not an FMI 3.0 conformance "
            "claim and says nothing about other exporters or other FMUs."
        ),
        "established_by": [
            "archives.json (schema validation, declared capabilities, exported symbols)",
            "acceptance-report.json (executed Runs from the installed bundle)",
            "references/ (independent FMPy trajectories recorded during preparation)",
        ],
        "supported": {
            "exchange": "one constant communication interval per Run, set by the participant Period",
            "lifecycle": list(EXERCISED_SYMBOLS),
            "variables": "Float64 scalars with declared SI units and input starts",
            "resource_path": (
                "absolute extracted resources/ directory with a trailing separator, "
                "supplied by the Importer"
            ),
            "platform": "x86_64-linux binaries hosted by the image's CPython 3.13",
        },
        "rejected": [
            {
                "capability": "canHandleVariableCommunicationStepSize",
                "declared": "false",
                "consequence": "each communication interval is a separate Run; no adaptive stepping",
            },
            {
                "capability": "canGetAndSetFMUState",
                "declared": "false",
                "consequence": "no rollback, no state snapshot, no repeated Step from one instant",
            },
            {
                "capability": "canSerializeFMUState",
                "declared": "false",
                "consequence": "Run state cannot be carried between processes or hosts",
            },
            {
                "capability": "needsExecutionTool",
                "declared": "true",
                "consequence": "the archive carries no Python runtime; the image supplies CPython",
            },
        ],
        "not_exercised": [
            "Model Exchange and Scheduled Execution interfaces",
            "Event Mode, Clocks and intermediate update",
            "early return from fmi3DoStep",
            "Binary, String, Enumeration and array variables",
            "non-Float64 numeric types",
            "platforms other than Linux x86-64",
        ],
    }


def handoff() -> dict:
    """Baseline signals and KPIs a CAN-connected ADAS regression can reuse."""
    nominal = scenario_configuration("nominal")
    return {
        "purpose": (
            "The measured baseline a later CAN-connected ADAS regression compares "
            "against. Signal names, units, rates and thresholds are reused as they "
            "stand; only the transport below them changes."
        ),
        "time": {
            "unit": "ns",
            "epoch_ns": 0,
            "publication_time": "the activation Slot a Message was published in",
            "endpoint_time": "publication Slot plus the publishing participant Period",
            "default_channel_latency": (
                "one consumer activation (unit delay) when a Channel declares no "
                "latency_ns; every Channel in this bundle declares one explicitly"
            ),
        },
        "signals": [
            {
                "channel": channel,
                "fields": SCENARIO_FIELDS[channel],
                "rate_hz": nominal["rates_hz"].get(
                    {"truth": "sensing", "command": "command",
                     "sensing": "sensing", "maneuver": "maneuver",
                     "freshness": "kpi"}[channel]
                ),
                "latency_ns": 0 if channel == "maneuver" else nominal["step_ns"],
                "source": {
                    "sensing": "AccPlant FMU outputs the controller consumes",
                    "truth": "AccPlant FMU outputs no fault can hide",
                    "command": "AccController FMU output",
                    "maneuver": "authored lead-acceleration input",
                    "freshness": "Test participant; time since the last sensing delivery",
                }[channel],
            }
            for channel in SCENARIO_FIELDS
        ],
        "units": nominal["units"],
        "kpis": {
            "minimum_gap_m": nominal["kpi"]["minimum_gap_m"],
            "acceleration_min_mps2": nominal["kpi"]["acceleration_min_mps2"],
            "acceleration_max_mps2": nominal["kpi"]["acceleration_max_mps2"],
            "tracking_start_ns": nominal["kpi"]["tracking_start_ns"],
            "spacing_error_max_m": nominal["kpi"]["spacing_error_max_m"],
            "relative_speed_max_mps": nominal["kpi"]["relative_speed_max_mps"],
            "desired_spacing_m": "5 + 1.5 * max(ego_speed_mps, 0)",
            "zero_speed_policy": nominal["zero_speed_policy"],
        },
        "numerical_thresholds": {
            "comparison_absolute_si": nominal["absolute_tolerance"],
            "comparison_relative": nominal["relative_tolerance"],
            "independent_reference_absolute_si": REFERENCE_ABS_TOL,
            "sensitivity_acceptance_envelope": ACCEPTANCE_ENVELOPE,
        },
        "timing": {
            "scenario_step_ns": nominal["step_ns"],
            "scenario_duration_ns": nominal["duration_ns"],
            "sensitivity_maneuver_period_ns": MANEUVER_PERIOD_NS,
            "sensitivity_channels": {
                channel: CHANNEL_FIELDS[channel] for channel in CHANNEL_FIELDS
            },
        },
        "not_carried_over": [
            "CAN frame layout, DBC signal packing and bus arbitration",
            "sensor perception and vehicle-physics fidelity",
            "any safety claim; the minimum-gap KPI is an integration threshold",
        ],
        "minimum_gap_threshold_m": MIN_GAP_M,
    }
