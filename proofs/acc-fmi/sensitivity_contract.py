"""Authored matrix for the closed-loop communication-interval experiment.

This module is deliberately data-first.  ``configuration()`` is written before
any independent reference or SiL Run is collected, so the report can never
silently acquire a period or Latency from an observed artifact.
"""
from __future__ import annotations

from copy import deepcopy

MS = 1_000_000
SECOND = 1_000 * MS
RATES_NS = (20 * MS, 10 * MS, 5 * MS)
BASE_NS = 10 * MS
REFERENCE_NS = 1 * MS
DURATION_NS = 2 * SECOND
CONSTANT_DURATION_NS = SECOND
OBSERVATION_GRID_NS = REFERENCE_NS
KPI_PERIOD_NS = 20 * MS
MIN_GAP_M = 5.0
CONSTANT_ACCELERATION_MPS2 = 1.5
REFERENCE_ABS_TOL = 1e-9

SENSING_FIELDS = ["gap_m", "relative_speed_mps", "ego_speed_mps"]
STATE_FIELDS = [
    "ego_position_m", "ego_speed_mps", "lead_position_m", "lead_speed_mps",
    "gap_m", "relative_speed_mps",
]
COMMAND_FIELDS = ["accel_mps2"]
CHANNEL_FIELDS = {
    "sensing": SENSING_FIELDS,
    "state": STATE_FIELDS,
    "command": COMMAND_FIELDS,
}

INITIAL_STATE = {
    "ego_position_m": 0.0,
    "ego_speed_mps": 25.0,
    "lead_position_m": 60.0,
    "lead_speed_mps": 25.0,
}

# A 20 ms hold at the initial 25 m/s covers 0.5 m.  The speed and command
# allowances cover one such interval at the plant's ±3 m/s² command envelope.
# These are an acceptance envelope for the declared rate matrix, not a claim
# that a sampled nonlinear controller must converge monotonically.
ACCEPTANCE_ENVELOPE = {
    "gap_m": 0.5,
    "ego_position_m": 0.5,
    "lead_position_m": 0.5,
    "ego_speed_mps": 0.1,
    "lead_speed_mps": 0.1,
    "relative_speed_mps": 0.1,
    "accel_mps2": 0.1,
    "minimum_gap_m": 0.5,
}


def _row(
    name: str,
    family: str,
    scenario: str,
    plant_period_ns: int | None,
    controller_period_ns: int | None,
    sensing_latency_ns: int | None,
    command_latency_ns: int | None,
    *,
    duration_ns: int = DURATION_NS,
    reference: str,
    negative_control: bool = False,
    sensitivity_baseline: str | None = None,
    initial_command_mps2: float = 0.0,
) -> dict:
    return {
        "name": name,
        "family": family,
        "scenario": scenario,
        "duration_ns": duration_ns,
        "observation_grid_ns": OBSERVATION_GRID_NS,
        "periods_ns": {
            "plant": plant_period_ns,
            "controller": controller_period_ns,
            "kpi": KPI_PERIOD_NS if controller_period_ns is not None else None,
        },
        "latencies_ns": {
            "sensing": sensing_latency_ns,
            "command": command_latency_ns,
            "state": 0 if plant_period_ns is not None else None,
        },
        "modeled_physical_delay_ns": 0,
        "initial_command_mps2": initial_command_mps2,
        "reference": reference,
        "negative_control": negative_control,
        "sensitivity_baseline": sensitivity_baseline,
    }


def _rate_name(prefix: str, period_ns: int) -> str:
    return f"{prefix}-{period_ns // MS}ms"


def measured_rows() -> list[dict]:
    rows = []
    rows.extend(
        _row(
            _rate_name("constant", period),
            "integration-refinement",
            "constant-acceleration",
            period,
            None,
            None,
            None,
            duration_ns=CONSTANT_DURATION_NS,
            reference="analytic",
            initial_command_mps2=CONSTANT_ACCELERATION_MPS2,
        )
        for period in RATES_NS
    )
    rows.extend(
        _row(
            _rate_name("combined", period),
            "combined-sensitivity",
            "changing-input",
            period,
            period,
            period,
            period,
            reference="fine-combined-1ms",
        )
        for period in RATES_NS
    )
    rows.extend(
        _row(
            _rate_name("sampling", period),
            "controller-sampling",
            "changing-input",
            BASE_NS,
            period,
            BASE_NS,
            BASE_NS,
            reference="fine-sampling-1ms",
        )
        for period in RATES_NS
    )
    rows.extend(
        _row(
            f"latency-{latency // MS}ms",
            "channel-latency",
            "changing-input",
            BASE_NS,
            BASE_NS,
            latency,
            latency,
            reference=f"fine-latency-{latency // MS}ms",
            sensitivity_baseline="latency-10ms",
        )
        for latency in (0, BASE_NS, 20 * MS)
    )
    rows.append(
        _row(
            "timing-defect",
            "negative-control",
            "changing-input",
            BASE_NS,
            BASE_NS,
            100 * MS,
            BASE_NS,
            reference="fine-timing-defect",
            negative_control=True,
            sensitivity_baseline="latency-10ms",
        )
    )
    return rows


def reference_rows() -> list[dict]:
    return [
        _row(
            "fine-combined-1ms",
            "independent-reference",
            "changing-input",
            REFERENCE_NS,
            REFERENCE_NS,
            REFERENCE_NS,
            REFERENCE_NS,
            reference="self",
        ),
        _row(
            "fine-sampling-1ms",
            "independent-reference",
            "changing-input",
            BASE_NS,
            REFERENCE_NS,
            BASE_NS,
            BASE_NS,
            reference="self",
        ),
        *[
            _row(
                f"fine-latency-{latency // MS}ms",
                "independent-reference",
                "changing-input",
                REFERENCE_NS,
                REFERENCE_NS,
                latency,
                latency,
                reference="self",
            )
            for latency in (0, BASE_NS, 20 * MS)
        ],
        _row(
            "fine-timing-defect",
            "independent-reference",
            "changing-input",
            REFERENCE_NS,
            REFERENCE_NS,
            100 * MS,
            BASE_NS,
            reference="self",
        ),
    ]


def row(name: str) -> dict:
    for item in measured_rows() + reference_rows():
        if item["name"] == name:
            return deepcopy(item)
    raise KeyError(name)


def configuration() -> dict:
    return {
        "experiment": "closed-loop communication-interval sensitivity",
        "schema_version": 1,
        "duration_policy": (
            "Each row owns its declared Duration; changing-input rows cover "
            "[0, 2 s), constant-acceleration rows cover [0, 1 s)."
        ),
        "equations": {
            "plant": (
                "x(t+h)=x(t)+v(t)h+0.5*a*h*h; "
                "v(t+h)=v(t)+a*h"
            ),
            "gap": "lead_position_m-ego_position_m",
            "relative_speed": "lead_speed_mps-ego_speed_mps",
            "controller": (
                "clamp(0.35*(gap-(5+1.5*ego_speed)) + "
                "1.2*relative_speed, -3, 1.5)"
            ),
        },
        "exchange_algorithm": [
            "Open the next Slot at the smallest due participant activation.",
            "Run due activations by priority: plant, controller, KPI.",
            "Drain each visible FIFO in publish order before its participant Step.",
            "Write the newest held input, execute one fixed FMI communication interval,",
            "and publish post-step outputs in the activation Slot.",
        ],
        "sample_hold_rules": [
            "Plant holds the newest command until a command Message arrives.",
            "Controller holds the newest sensing values until a sensing Message arrives.",
            "FMU outputs are published at the Slot where the post-step values are observed.",
            "A measured value is compared to a reference only at the exact endpoint time it names.",
            "No interpolation is performed across missing or discontinuous samples.",
        ],
        "initial_state": INITIAL_STATE,
        "initial_outputs": {
            "sensing": {"gap_m": 60.0, "relative_speed_mps": 0.0, "ego_speed_mps": 25.0},
            "state": INITIAL_STATE,
            "controller_command_mps2": 1.5,
        },
        "initial_command_mps2": 0.0,
        "constant_acceleration_mps2": CONSTANT_ACCELERATION_MPS2,
        "observation_grid_ns": OBSERVATION_GRID_NS,
        "comparison_tolerances": {
            "absolute_si": REFERENCE_ABS_TOL,
            "relative": 0.0,
            "policy": "independent-reference matching uses the absolute budget; the sensitivity envelope is separate.",
        },
        "kpi": {
            "name": "minimum-gap",
            "threshold_m": MIN_GAP_M,
            "post_hoc_fields": ["minimum_gap_m", "maximum_ego_speed_mps", "maximum_command_mps2"],
        },
        "acceptance_envelope": ACCEPTANCE_ENVELOPE,
        "timing_contract": {
            "can_handle_variable_step_size": False,
            "run_contract": "one constant FMI communication interval per Run",
            "physical_delay_ns": 0,
            "finer_reference": (
                "The fixed-period FMUs are exercised in separate 1 ms Runs; "
                "this is not a variable-step claim."
            ),
        },
        "limitations": {
            "fixed_controller_sampling_in_numeric_refinement": False,
            "controller_sampling_in_integration_family": "not applicable: no controller is present",
            "numeric_refinement_note": (
                "The qualified FMUs advertise no variable communication-step "
                "capability. Integration-only refinement is therefore the "
                "standalone constant-acceleration plant family. Closed-loop "
                "20/10/5 ms rows that change plant interval, controller sampling, "
                "and Channel Latency together are explicitly classified as "
                "combined sensitivity experiments."
            ),
            "modeled_physical_delays": "The qualified ACC FMUs declare no physical delay; all rows use 0 ns.",
        },
        "rows": measured_rows(),
        "independent_reference_rows": reference_rows(),
    }
