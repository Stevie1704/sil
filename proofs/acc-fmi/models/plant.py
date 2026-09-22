"""Two vehicles with exact constant-acceleration motion per FMI interval."""
from pythonfmu3 import Fmi3Slave, Fmi3Causality, Float64, Unit
from dynamics import (advance, EGO_POSITION_M, EGO_SPEED_MPS, LEAD_POSITION_M,
                      LEAD_SPEED_MPS, LEAD_ACCEL_MPS2, INITIAL_COMMAND_MPS2)


class AccPlant(Fmi3Slave):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.license = "Apache-2.0"
        self.time = 0.0
        self.register_variable(Float64("time", unit="s", causality=Fmi3Causality.independent))
        self.register_units([Unit("s", s=1), Unit("m", m=1), Unit("m/s", m=1, s=-1),
                             Unit("m/s2", m=1, s=-2)])
        self.accel_mps2 = INITIAL_COMMAND_MPS2
        self.ego_position_m = EGO_POSITION_M
        self.ego_speed_mps = EGO_SPEED_MPS
        self.lead_position_m = LEAD_POSITION_M
        self.lead_speed_mps = LEAD_SPEED_MPS
        self.register_variable(Float64("accel_mps2", unit="m/s2",
                                       causality=Fmi3Causality.input))
        for name, unit in [("ego_position_m", "m"), ("ego_speed_mps", "m/s"),
                           ("lead_position_m", "m"), ("lead_speed_mps", "m/s"),
                           ("gap_m", "m"), ("relative_speed_mps", "m/s")]:
            self.register_variable(Float64(name, unit=unit, causality=Fmi3Causality.output))

    @property
    def gap_m(self):
        return self.lead_position_m - self.ego_position_m

    @property
    def relative_speed_mps(self):
        return self.lead_speed_mps - self.ego_speed_mps

    def do_step(self, current_time, step_size):
        self.time = current_time + step_size
        self.ego_position_m, self.ego_speed_mps = advance(
            self.ego_position_m, self.ego_speed_mps, self.accel_mps2, step_size)
        self.lead_position_m, self.lead_speed_mps = advance(
            self.lead_position_m, self.lead_speed_mps, LEAD_ACCEL_MPS2, step_size)
        return True
