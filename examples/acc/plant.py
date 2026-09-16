"""Compatibility entry point for the packaged ACC plant."""

from sil.examples.acc.plant import (
    EGO_POSITION_M,
    EGO_SPEED_MPS,
    INITIAL_COMMAND_MPS2,
    LEAD_ACCEL_MPS2,
    LEAD_POSITION_M,
    LEAD_SPEED_MPS,
    NS_PER_S,
    Plant,
    advance,
)
from sil.participant import StepParticipant, run


if __name__ == "__main__":
    run(Plant())
