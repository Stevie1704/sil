"""Compatibility entry point for the packaged ACC controller."""

from sil.examples.acc.controller import (
    GAP_GAIN_PER_S2,
    MAX_ACCEL_MPS2,
    MIN_ACCEL_MPS2,
    RELATIVE_SPEED_GAIN_PER_S,
    STANDSTILL_GAP_M,
    TIME_HEADWAY_S,
    Controller,
    command_for,
)
from sil.participant import StepParticipant, run


if __name__ == "__main__":
    run(Controller())
