"""Sampled ACC law; FMI execution is supplied by PythonFMU3."""
from pythonfmu3 import Fmi3Slave, Fmi3Causality, Float64, Unit
from dynamics import command_for


class AccController(Fmi3Slave):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.license = "Apache-2.0"
        self.time = 0.0
        self.register_variable(Float64("time", unit="s", causality=Fmi3Causality.independent))
        self.register_units([Unit("s", s=1), Unit("m", m=1), Unit("m/s", m=1, s=-1),
                             Unit("m/s2", m=1, s=-2)])
        self.gap_m = 60.0
        self.relative_speed_mps = 0.0
        self.ego_speed_mps = 25.0
        self.accel_mps2 = 0.0
        for name, unit in [("gap_m", "m"), ("relative_speed_mps", "m/s"),
                           ("ego_speed_mps", "m/s")]:
            self.register_variable(Float64(name, unit=unit, causality=Fmi3Causality.input))
        self.register_variable(Float64("accel_mps2", unit="m/s2",
                                       causality=Fmi3Causality.output))

    def exit_initialization_mode(self):
        self._evaluate()

    def _evaluate(self):
        self.accel_mps2 = command_for(self.gap_m, self.relative_speed_mps,
                                      self.ego_speed_mps)

    def do_step(self, current_time, step_size):
        self.time = current_time + step_size
        self._evaluate()
        return True
