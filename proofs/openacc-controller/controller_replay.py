"""The OpenACC replay into `AccController`: conversion, Run and judgement (#194).

Everything a Run and its judgement decide is stated here once, so the
authoring documents, the conversion mappings, the comparison contract and the
expected control failures cannot disagree. The values are the #178 handoff's
(`single_fmu_194`); a test holds them to the committed file.

- **Conversion.** The bundle's window CSV keeps the recorded columns next to
  the derived ones. `recorded_inputs` recomputes every derived column from
  the recorded ones and refuses a difference: `gap_m` = `IVS1`,
  `relative_speed_mps` = `Speed1 - Speed2`, `ego_speed_mps` = `Speed2`, and
  `t_ns` on the exact 100 ms grid from the window start. `sil recording csv` then
  converts the derived columns into `acc.sensing`, with `t_ns` as the Message
  time.
- **Input.** The Message of sample k is published at t_k and, with Latency
  0, is written to the FMU before the Step [t_k, t_k+1). The start values are
  sample 0.
- **Observation.** The command published in Slot t_k is the value at
  t_k + 100 ms. The contract compares it with the reference row at that time,
  every row from 100 ms through 50.1 s.
- **Comparison.** `abs(actual - reference) <= 1e-10 + 1e-12 * abs(reference)`,
  `sil compare`'s rule.

Each control changes one thing and must fail where the control law, applied
to what the FMU sees under that change, first leaves the tolerance. That place
is computed before any Run:

| Control | Change | What the FMU sees |
| --- | --- | --- |
| `changed-input` | `relative_speed_mps` converted with scale -1 | `Speed2 - Speed1` |
| `wrong-binding` | the two speed fields bound to each other's variable | ego and relative speed swapped |
| `one-period-shift` | `acc.sensing` Latency one period (route capacity 2) | sample k-1 in Step k; sample 0 in Step 0 |

`declared-starts` is not a failing control. It replaces the start values with
the FMU's declared ones and must pass: at Latency 0 sample 0 overwrites every
start value before the first Step, and the controller holds no state. A
wrong start value is therefore not observable, and the wrong-parameter control
is a binding error instead.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace

PERIOD_NS = 100_000_000
DURATION_NS = 50_100_000_000
ABS_TOL = 1e-10
REL_TOL = 1e-12

SENSING_CHANNEL = "acc.sensing"
COMMAND_CHANNEL = "acc.command"
REFERENCE_CHANNEL = "reference.command"
SENSING_SCHEMA = "acc.Sensing"
COMMAND_SCHEMA = "acc.Command"
INPUTS = ("gap_m", "relative_speed_mps", "ego_speed_mps")
OUTPUT = "accel_mps2"
UNITS = {"gap_m": "m", "relative_speed_mps": "m/s", "ego_speed_mps": "m/s",
         OUTPUT: "m/s2"}

SCHEMAS = {
    SENSING_SCHEMA: {"fields": [{"name": n, "type": "f64"} for n in INPUTS]},
    COMMAND_SCHEMA: {"fields": [{"name": OUTPUT, "type": "f64"}]},
}


@dataclass(frozen=True)
class Variant:
    """One Run of the workload: the nominal one, or one change to it."""

    relative_speed_scale: int = 1
    input_latency_ns: int = 0
    swap_speeds: bool = False
    declared_starts: bool = False

    def seen(self, samples: list[dict]) -> list[dict]:
        """The input values the FMU holds in each Step."""
        converted = [self._bound({**s, "relative_speed_mps":
                                  s["relative_speed_mps"] * self.relative_speed_scale})
                     for s in samples]
        if self.input_latency_ns == 0:
            return converted
        if self.input_latency_ns != PERIOD_NS or self.declared_starts:
            raise ValueError("only a one-period shift from sample-0 starts "
                             "is predicted")
        # Step 0 holds the start values, and they are sample 0 by variable.
        return [samples[0], *converted[:-1]]

    def _bound(self, sample: dict) -> dict:
        if not self.swap_speeds:
            return sample
        return {**sample, "relative_speed_mps": sample["ego_speed_mps"],
                "ego_speed_mps": sample["relative_speed_mps"]}


def recording_stem(variant: Variant) -> str:
    """The converted input a variant replays: only the scale changes it."""
    return "sensing" if variant.relative_speed_scale == 1 else "sensing-changed"


NOMINAL = Variant()
CONTROLS = {
    "changed-input": replace(NOMINAL, relative_speed_scale=-1),
    "wrong-binding": replace(NOMINAL, swap_speeds=True),
    "one-period-shift": replace(NOMINAL, input_latency_ns=PERIOD_NS),
    "declared-starts": replace(NOMINAL, declared_starts=True),
}


# Conversion ---------------------------------------------------------------------

def recorded_inputs(text: str, window_start_s: float) -> list[dict]:
    """The controller inputs of the window CSV, each derived column checked
    against the recorded columns it comes from."""
    start_ms = round(window_start_s * 1000)
    samples = []
    for index, row in enumerate(csv.DictReader(io.StringIO(text))):
        recorded = {name: float(row[name])
                    for name in ("Time", "Speed1", "Speed2", "IVS1")}
        t_ns = (round(recorded["Time"] * 1000) - start_ms) * 1_000_000
        if t_ns != index * PERIOD_NS:
            raise ValueError(f"row {index} at {row['Time']} s is off the "
                             f"{PERIOD_NS} ns grid")
        expected = {
            "t_ns": t_ns,
            "gap_m": recorded["IVS1"],
            "relative_speed_mps": recorded["Speed1"] - recorded["Speed2"],
            "ego_speed_mps": recorded["Speed2"],
        }
        derived = {"t_ns": int(row["t_ns"]),
                   **{name: float(row[name]) for name in INPUTS}}
        for name, value in expected.items():
            if derived[name] != value:
                raise ValueError(f"row {index}: {name} is {derived[name]!r}, "
                                 f"the policy gives {value!r}")
        samples.append(derived)
    return samples


def input_mapping(variant: Variant) -> dict:
    """The `sil recording csv` mapping from the window CSV to `acc.sensing`."""
    fields = {name: {"column": name} for name in INPUTS}
    if variant.relative_speed_scale != 1:
        fields["relative_speed_mps"]["scale"] = variant.relative_speed_scale
    return {
        "sil_csv_mapping": 1,
        "timestamp": {"column": "t_ns", "unit": "ns"},
        "schemas": {SENSING_SCHEMA: SCHEMAS[SENSING_SCHEMA]},
        "channels": [{"channel": SENSING_CHANNEL, "schema": SENSING_SCHEMA,
                      "fields": fields}],
    }


REFERENCE_MAPPING = {
    "sil_csv_mapping": 1,
    "timestamp": {"column": "t_ns", "unit": "ns"},
    "schemas": {COMMAND_SCHEMA: SCHEMAS[COMMAND_SCHEMA]},
    "channels": [{"channel": REFERENCE_CHANNEL, "schema": COMMAND_SCHEMA,
                  "fields": {OUTPUT: {"column": OUTPUT}}}],
}


def reference_rows(trace: list[dict]) -> list[dict]:
    """The FMPy reference as the CSV rows `REFERENCE_MAPPING` reads."""
    # repr is the shortest exact decimal of the binary64 value.
    return [{"t_ns": row["t_ns"], OUTPUT: repr(float(row[OUTPUT]))}
            for row in trace]


# Authoring ----------------------------------------------------------------------

def route_capacity(variant: Variant) -> int:
    """One Message per period, and each one in flight for its Latency: a
    Message published while the one before it is still in flight waits
    behind it."""
    return 1 + variant.input_latency_ns // PERIOD_NS


def authoring_document(variant: Variant, first: dict) -> dict:
    """The `sil fmi replay` document of one Run; `first` is sample 0."""
    variable = {name: name for name in INPUTS}
    if variant.swap_speeds:
        variable["relative_speed_mps"] = "ego_speed_mps"
        variable["ego_speed_mps"] = "relative_speed_mps"
    starts = [] if variant.declared_starts else [
        {"variable": name, "value": repr(float(first[name])), "unit": UNITS[name]}
        for name in INPUTS]
    return {
        "sil_fmu_replay": 1,
        "step_period_ns": PERIOD_NS,
        "duration_ns": DURATION_NS,
        "schemas": SCHEMAS,
        "channels": {
            SENSING_CHANNEL: {"schema": SENSING_SCHEMA, "direction": "in",
                              "latency_ns": variant.input_latency_ns,
                              "route": {"capacity": route_capacity(variant),
                                        "overflow": "fail"}},
            # Nothing subscribes; the command is recorded only.
            COMMAND_CHANNEL: {"schema": COMMAND_SCHEMA, "direction": "out",
                              "latency_ns": PERIOD_NS},
        },
        "bind": [
            *({"channel": SENSING_CHANNEL, "field": field,
               "variable": variable[field], "unit": UNITS[variable[field]]}
              for field in INPUTS),
            {"channel": COMMAND_CHANNEL, "field": OUTPUT, "variable": OUTPUT,
             "unit": UNITS[OUTPUT]},
        ],
        "start": starts,
        "hold": [],
    }


# Judgement ----------------------------------------------------------------------

def comparison_contract() -> dict:
    """Every command from 100 ms through the final one at 50.1 s."""
    return {
        "sil_comparison": 1,
        "evaluation": {"from_ns": PERIOD_NS, "to_ns": DURATION_NS},
        "channels": {COMMAND_CHANNEL: {
            "reference_channel": REFERENCE_CHANNEL,
            "actual_offset_ns": PERIOD_NS,
            "reference_offset_ns": 0,
            "observations": {"start_ns": PERIOD_NS, "stop_ns": DURATION_NS,
                             "step_ns": PERIOD_NS},
            "fields": {OUTPUT: {"atol": ABS_TOL, "rtol": REL_TOL}},
        }},
    }


def first_divergence(reference: list[dict], commands: list[float]) -> dict | None:
    """The first command outside the contract's tolerance of its reference."""
    for index, (row, actual) in enumerate(zip(reference, commands, strict=True)):
        expected = row[OUTPUT]
        if not abs(actual - expected) <= ABS_TOL + REL_TOL * abs(expected):
            return {"index": index, "observation_ns": row["t_ns"],
                    "expected": expected, "actual": actual}
    return None


def predicted_divergence(variant: Variant, samples: list[dict],
                         reference: list[dict], law) -> dict | None:
    """Where the Run of `variant` must first diverge: the law over what the
    FMU sees, against the reference."""
    commands = [law(**{name: s[name] for name in INPUTS})
                for s in variant.seen(samples)]
    return first_divergence(reference, commands)
