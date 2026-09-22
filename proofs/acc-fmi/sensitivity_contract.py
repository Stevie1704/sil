"""Authored contract for the closed-loop communication-period experiment."""
from __future__ import annotations

from dataclasses import dataclass

MS = 1_000_000
SECOND = 1_000 * MS
PERIODS_NS = (20 * MS, 10 * MS, 5 * MS)
BASE_PERIOD_NS = 10 * MS
MANEUVER_PERIOD_NS = 1 * MS
REFERENCE_PERIOD_NS = 1 * MS
DURATION_NS = 2 * SECOND
KPI_PERIOD_NS = 20 * MS
TIMING_DEFECT_SENSING_LATENCY_NS = 250 * MS
MIN_GAP_M = 5.0
CONSTANT_ACCELERATION_MPS2 = 1.5
INITIAL_LEAD_POSITION_M = 44.0
INITIAL_EGO_POSITION_M = 0.0
INITIAL_SPEED_MPS = 25.0
REFERENCE_ABS_TOL = 1e-9

SENSING_FIELDS = ["gap_m", "relative_speed_mps", "ego_speed_mps"]
STATE_FIELDS = [
    "ego_position_m", "ego_speed_mps", "lead_position_m", "lead_speed_mps",
    "gap_m", "relative_speed_mps",
]
COMMAND_FIELDS = ["accel_mps2"]
MANEUVER_FIELDS = ["lead_accel_mps2"]
CHANNEL_FIELDS = {
    "sensing": SENSING_FIELDS,
    "state": STATE_FIELDS,
    "command": COMMAND_FIELDS,
    "maneuver": MANEUVER_FIELDS,
}

INITIAL_STATE = {
    "ego_position_m": INITIAL_EGO_POSITION_M,
    "ego_speed_mps": INITIAL_SPEED_MPS,
    "lead_position_m": INITIAL_LEAD_POSITION_M,
    "lead_speed_mps": INITIAL_SPEED_MPS,
}
INITIAL_OUTPUT_STATE = {
    **INITIAL_STATE,
    "gap_m": INITIAL_LEAD_POSITION_M - INITIAL_EGO_POSITION_M,
    "relative_speed_mps": 0.0,
}
INITIAL_SENSING = {
    "gap_m": INITIAL_OUTPUT_STATE["gap_m"],
    "relative_speed_mps": 0.0,
    "ego_speed_mps": INITIAL_SPEED_MPS,
}
INITIAL_CONTROLLER_COMMAND_MPS2 = 0.525
INITIAL_COMMAND_MPS2 = 0.0

# Transitions intentionally miss the 5/10/20 ms plant grids. The 1 ms
# Maneuver participant publishes the left-held value on each 1 ms Slot.
MANEUVER_SEGMENTS = (
    (0, 237 * MS, 0.0),
    (237 * MS, 613 * MS, 1.5),
    (613 * MS, 1_087 * MS, -1.5),
    (1_087 * MS, 1_463 * MS, 0.75),
    (1_463 * MS, DURATION_NS, -0.75),
)

# Declared before any Run. These are acceptance bounds for the authored normal
# matrix, not fitted tolerances or a claim of monotonic convergence.
ACCEPTANCE_ENVELOPE = {
    "gap_m": 1.0,
    "ego_position_m": 1.0,
    "lead_position_m": 1.0,
    "ego_speed_mps": 0.5,
    "lead_speed_mps": 0.5,
    "relative_speed_mps": 0.5,
    "accel_mps2": 0.5,
    # The authored Maneuver participant is part of the exact-time comparison;
    # its schedule must not be silently replaced by a resampled input.
    "lead_accel_mps2": 1e-12,
    "minimum_gap_m": 1.0,
}


def lead_acceleration_at_ns(time_ns: int) -> float:
    """The authored input value visible on the Maneuver Channel at ``time_ns``."""
    for start_ns, end_ns, acceleration in MANEUVER_SEGMENTS:
        if start_ns <= time_ns < end_ns:
            return acceleration
    return MANEUVER_SEGMENTS[-1][2]


@dataclass(frozen=True)
class SensitivityRow:
    """One fully declared Run family member."""

    name: str
    family: str
    kind: str
    duration_ns: int
    plant_period_ns: int
    controller_period_ns: int | None
    maneuver_period_ns: int | None
    kpi_period_ns: int | None
    sensing_latency_ns: int | None
    command_latency_ns: int | None
    maneuver_latency_ns: int | None
    state_latency_ns: int | None
    reference: str
    sensitivity_baseline: str | None = None
    negative_control: bool = False
    initial_command_mps2: float = INITIAL_COMMAND_MPS2

    @property
    def periods(self) -> dict[str, int | None]:
        return {
            "plant": self.plant_period_ns,
            "controller": self.controller_period_ns,
            "maneuver": self.maneuver_period_ns,
            "kpi": self.kpi_period_ns,
        }

    @property
    def latencies(self) -> dict[str, int | None]:
        return {
            "sensing": self.sensing_latency_ns,
            "command": self.command_latency_ns,
            "maneuver": self.maneuver_latency_ns,
            "state": self.state_latency_ns,
        }

    @property
    def closed_loop(self) -> bool:
        return self.controller_period_ns is not None

    @property
    def has_maneuver(self) -> bool:
        return self.maneuver_period_ns is not None

    def to_document(self) -> dict:
        return {
            "name": self.name,
            "family": self.family,
            "kind": self.kind,
            "duration_ns": self.duration_ns,
            "observation_grid_ns": REFERENCE_PERIOD_NS,
            "periods_ns": self.periods,
            "latencies_ns": self.latencies,
            "modeled_physical_delay_ns": 0,
            "initial_command_mps2": self.initial_command_mps2,
            "reference": self.reference,
            "sensitivity_baseline": self.sensitivity_baseline,
            "negative_control": self.negative_control,
        }


def _period_name(prefix: str, period_ns: int) -> str:
    return f"{prefix}-{period_ns // MS}ms"


def _closed_loop_row(
    name: str,
    family: str,
    kind: str,
    *,
    plant_period_ns: int,
    controller_period_ns: int,
    sensing_latency_ns: int,
    command_latency_ns: int,
    reference: str,
    sensitivity_baseline: str | None = None,
    negative_control: bool = False,
) -> SensitivityRow:
    return SensitivityRow(
        name=name,
        family=family,
        kind=kind,
        duration_ns=DURATION_NS,
        plant_period_ns=plant_period_ns,
        controller_period_ns=controller_period_ns,
        maneuver_period_ns=MANEUVER_PERIOD_NS,
        kpi_period_ns=KPI_PERIOD_NS,
        sensing_latency_ns=sensing_latency_ns,
        command_latency_ns=command_latency_ns,
        maneuver_latency_ns=0,
        state_latency_ns=0,
        reference=reference,
        sensitivity_baseline=sensitivity_baseline,
        negative_control=negative_control,
    )


def measured_rows() -> tuple[SensitivityRow, ...]:
    rows = [
        SensitivityRow(
            name=_period_name("constant", period),
            family="analytic-oracle",
            kind="constant-acceleration",
            duration_ns=DURATION_NS,
            plant_period_ns=period,
            controller_period_ns=None,
            maneuver_period_ns=None,
            kpi_period_ns=None,
            sensing_latency_ns=None,
            command_latency_ns=None,
            maneuver_latency_ns=None,
            state_latency_ns=0,
            reference="analytic-constant",
            initial_command_mps2=CONSTANT_ACCELERATION_MPS2,
        )
        for period in PERIODS_NS
    ]
    rows.extend(
        SensitivityRow(
            name=_period_name("forcing", period),
            family="plant-forcing-oracle",
            kind="changing-input",
            duration_ns=DURATION_NS,
            plant_period_ns=period,
            controller_period_ns=None,
            maneuver_period_ns=MANEUVER_PERIOD_NS,
            kpi_period_ns=None,
            sensing_latency_ns=None,
            command_latency_ns=None,
            maneuver_latency_ns=0,
            state_latency_ns=0,
            reference="analytic-forcing",
        )
        for period in PERIODS_NS
    )
    rows.extend(
        _closed_loop_row(
            _period_name("plant", period),
            "plant-period-sensitivity",
            "changing-input",
            plant_period_ns=period,
            controller_period_ns=BASE_PERIOD_NS,
            sensing_latency_ns=BASE_PERIOD_NS,
            command_latency_ns=BASE_PERIOD_NS,
            reference="fine-plant-1ms",
        )
        for period in PERIODS_NS
    )
    rows.extend(
        _closed_loop_row(
            _period_name("sampling", period),
            "controller-sampling",
            "changing-input",
            plant_period_ns=BASE_PERIOD_NS,
            controller_period_ns=period,
            sensing_latency_ns=BASE_PERIOD_NS,
            command_latency_ns=BASE_PERIOD_NS,
            reference="fine-sampling-1ms",
        )
        for period in PERIODS_NS
    )
    rows.extend(
        _closed_loop_row(
            f"latency-{latency // MS}ms",
            "channel-latency",
            "changing-input",
            plant_period_ns=BASE_PERIOD_NS,
            controller_period_ns=BASE_PERIOD_NS,
            sensing_latency_ns=latency,
            command_latency_ns=latency,
            reference=f"fine-latency-{latency // MS}ms",
            sensitivity_baseline="latency-10ms",
        )
        for latency in (0, BASE_PERIOD_NS, 20 * MS)
    )
    rows.extend(
        _closed_loop_row(
            _period_name("combined", period),
            "combined-sensitivity",
            "changing-input",
            plant_period_ns=period,
            controller_period_ns=period,
            sensing_latency_ns=period,
            command_latency_ns=period,
            reference="fine-combined-1ms",
        )
        for period in PERIODS_NS
    )
    rows.append(
        _closed_loop_row(
            "timing-defect",
            "negative-control",
            "changing-input",
            plant_period_ns=BASE_PERIOD_NS,
            controller_period_ns=BASE_PERIOD_NS,
            sensing_latency_ns=TIMING_DEFECT_SENSING_LATENCY_NS,
            command_latency_ns=BASE_PERIOD_NS,
            reference="fine-timing-defect",
            sensitivity_baseline="latency-10ms",
            negative_control=True,
        )
    )
    return tuple(rows)


def reference_rows() -> tuple[SensitivityRow, ...]:
    return (
        _closed_loop_row(
            "fine-plant-1ms", "independent-reference", "changing-input",
            plant_period_ns=REFERENCE_PERIOD_NS,
            controller_period_ns=BASE_PERIOD_NS,
            sensing_latency_ns=BASE_PERIOD_NS,
            command_latency_ns=BASE_PERIOD_NS,
            reference="self",
        ),
        _closed_loop_row(
            "fine-sampling-1ms", "independent-reference", "changing-input",
            plant_period_ns=BASE_PERIOD_NS,
            controller_period_ns=REFERENCE_PERIOD_NS,
            sensing_latency_ns=BASE_PERIOD_NS,
            command_latency_ns=BASE_PERIOD_NS,
            reference="self",
        ),
        *(
            _closed_loop_row(
                f"fine-latency-{latency // MS}ms",
                "independent-reference",
                "changing-input",
                plant_period_ns=REFERENCE_PERIOD_NS,
                controller_period_ns=REFERENCE_PERIOD_NS,
                sensing_latency_ns=latency,
                command_latency_ns=latency,
                reference="self",
            )
            for latency in (0, BASE_PERIOD_NS, 20 * MS)
        ),
        _closed_loop_row(
            "fine-combined-1ms", "independent-reference", "changing-input",
            plant_period_ns=REFERENCE_PERIOD_NS,
            controller_period_ns=REFERENCE_PERIOD_NS,
            sensing_latency_ns=REFERENCE_PERIOD_NS,
            command_latency_ns=REFERENCE_PERIOD_NS,
            reference="self",
        ),
        _closed_loop_row(
            "fine-timing-defect", "independent-reference", "changing-input",
            plant_period_ns=REFERENCE_PERIOD_NS,
            controller_period_ns=REFERENCE_PERIOD_NS,
            sensing_latency_ns=TIMING_DEFECT_SENSING_LATENCY_NS,
            command_latency_ns=BASE_PERIOD_NS,
            reference="self",
        ),
    )


def configuration() -> dict:
    return {
        "experiment": "closed-loop communication-period sensitivity",
        "duration_policy": "Every row covers [0, 2 s) and ends on its declared participant periods.",
        "equations": {
            "plant": (
                "p_next=p+v*h+0.5*a*h*h; v_next=v+a*h for the held input; "
                "lead acceleration is the Maneuver Channel value held by the FMU."
            ),
            "continuous_maneuver_oracle": (
                "The independent plant-only oracle integrates each piecewise-constant "
                "lead acceleration segment over the exact interval."
            ),
            "gap": "lead_position_m-ego_position_m",
            "relative_speed": "lead_speed_mps-ego_speed_mps",
            "controller": (
                "clamp(0.35*(gap-(5+1.5*ego_speed)) + "
                "1.2*relative_speed, -3, 1.5)"
            ),
        },
        "maneuver": {
            "channel": "maneuver",
            "period_ns": MANEUVER_PERIOD_NS,
            "latency_ns": 0,
            "fields": MANEUVER_FIELDS,
            "segments": [
                {"start_ns": start, "end_ns": end, "lead_accel_mps2": accel}
                for start, end, accel in MANEUVER_SEGMENTS
            ],
        },
        "periods_ns": list(PERIODS_NS),
        "initial_state": INITIAL_STATE,
        "initial_outputs": {
            "sensing": INITIAL_SENSING,
            "state": INITIAL_OUTPUT_STATE,
            "controller_command_mps2": INITIAL_CONTROLLER_COMMAND_MPS2,
        },
        "initial_command_mps2": INITIAL_COMMAND_MPS2,
        "constant_acceleration_mps2": CONSTANT_ACCELERATION_MPS2,
        "observation_grid_ns": REFERENCE_PERIOD_NS,
        "comparison_tolerances": {
            "absolute_si": REFERENCE_ABS_TOL,
            "relative": 0.0,
            "policy": "SiL and independent FMPy use the absolute budget; sensitivity uses the separate envelope.",
        },
        "kpi": {
            "name": "minimum-gap",
            "threshold_m": MIN_GAP_M,
            "period_ns": KPI_PERIOD_NS,
            "post_hoc_fields": [
                "minimum_gap_m", "maximum_ego_speed_mps", "maximum_command_mps2",
            ],
        },
        "acceptance_envelope": ACCEPTANCE_ENVELOPE,
        "acceptance_envelope_basis": {
            "normal_period_limit_ns": max(PERIODS_NS),
            "normal_latency_limit_ns": 20 * MS,
            "command_limit_mps2": 1.5,
            "policy": (
                "Fixed before any reference or Run: 1 m position/gap, 0.5 m/s "
                "speed, and 0.5 m/s2 command deviation are the normal-matrix "
                "observation budget for the declared 20 ms timing contract."
            ),
        },
        "timing_contract": {
            "can_handle_variable_step_size": False,
            "run_contract": "one constant FMI communication interval per Run",
            "physical_delay_ns": 0,
            "numeric_refinement": {
                "family": "plant-period-sensitivity",
                "controller_period_ns": BASE_PERIOD_NS,
                "sensing_latency_ns": BASE_PERIOD_NS,
                "command_latency_ns": BASE_PERIOD_NS,
            },
            "finer_reference": "Separate fixed-period 1 ms Runs; no variable-step FMI claim.",
        },
        "limitations": {
            "fixed_controller_sampling_in_numeric_refinement": True,
            "modeled_physical_delays": "The qualified ACC FMUs declare no physical delay; all rows use 0 ns.",
            "constant_oracle_role": "Constant acceleration is an analytic FMU sanity oracle; nonconstant Maneuver forcing supplies the plant-period sensitivity signal.",
            "negative_control": (
                f"The timing-defect row uses {TIMING_DEFECT_SENSING_LATENCY_NS // MS} ms "
                "sensing Latency and must exceed the predeclared envelope."
            ),
        },
        "rows": [row.to_document() for row in measured_rows()],
        "independent_reference_rows": [row.to_document() for row in reference_rows()],
    }
