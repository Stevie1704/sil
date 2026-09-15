"""Importer for FMI 3.0 co-simulation FMUs, run as a process participant.

The kernel learns nothing about FMI. A Manifest declares this module as a
process participant's command with an FMU path, and the step protocol drives
the FMU's co-simulation interface: its input variables are written from the
subscribed Channels, `fmi3DoStep` advances it, and its output variables are
published on the Channels it publishes.

Mapping needs no configuration beyond the Manifest. The initialization line
carries each Channel's schema and its direction, so a Channel's schema field
names are the FMU variable names and the direction decides which side of the
step the variable is touched on. The FMU path travels as a command argument,
which the Manifest already hashes, so nothing that affects the Run lives
outside the hashed Manifest.

Float64 variables only.
"""

from __future__ import annotations

import ctypes
import platform
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from sil import schema
from sil.participant import ParticipantFailure, StepParticipant, run

# Virtual time is integer nanoseconds; seconds are derived from those integers
# on every step and never accumulated, so the same integer always produces the
# same double. An integer denominator keeps that true past the 53 bits an
# int-to-float conversion holds exactly.
NS_PER_S = 1_000_000_000

# fmi3Status. Warning still carries a result; Discard, Error and Fatal do not.
_FMI_STATUS_NAMES = ("OK", "Warning", "Discard", "Error", "Fatal")
_FMI_FIRST_FAILING_STATUS = 2

# The FMU's `binaries/` subdirectory for the running platform, and the shared
# library suffix that goes with it.
_MACHINES = {"arm64": "aarch64", "AMD64": "x86_64"}
_SYSTEMS = {"Darwin": ("darwin", ".dylib"), "Linux": ("linux", ".so")}

_LOG_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p
)
_VALUE_REFERENCES = ctypes.POINTER(ctypes.c_uint32)
_FLOAT64_VALUES = ctypes.POINTER(ctypes.c_double)
_FLAG = ctypes.POINTER(ctypes.c_bool)

# The co-simulation entry points this importer drives, with the argument and
# return types ctypes cannot infer. An instance handle is a pointer: without a
# declared `c_void_p` restype ctypes would truncate it to an int.
_SIGNATURES = {
    "fmi3InstantiateCoSimulation": (
        ctypes.c_void_p,
        [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
         ctypes.c_bool, ctypes.c_bool, ctypes.c_bool, ctypes.c_bool,
         _VALUE_REFERENCES, ctypes.c_size_t,
         ctypes.c_void_p, _LOG_CALLBACK, ctypes.c_void_p],
    ),
    "fmi3EnterInitializationMode": (
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_bool, ctypes.c_double, ctypes.c_double,
         ctypes.c_bool, ctypes.c_double],
    ),
    "fmi3ExitInitializationMode": (ctypes.c_int, [ctypes.c_void_p]),
    "fmi3DoStep": (
        ctypes.c_int,
        [ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_bool,
         _FLAG, _FLAG, _FLAG, ctypes.POINTER(ctypes.c_double)],
    ),
    "fmi3GetFloat64": (
        ctypes.c_int,
        [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t,
         _FLOAT64_VALUES, ctypes.c_size_t],
    ),
    "fmi3SetFloat64": (
        ctypes.c_int,
        [ctypes.c_void_p, _VALUE_REFERENCES, ctypes.c_size_t,
         _FLOAT64_VALUES, ctypes.c_size_t],
    ),
    "fmi3Terminate": (ctypes.c_int, [ctypes.c_void_p]),
    "fmi3FreeInstance": (None, [ctypes.c_void_p]),
}


@dataclass(frozen=True)
class ModelDescription:
    """What `modelDescription.xml` says that driving the FMU depends on."""

    model_identifier: str
    instantiation_token: str
    float64_references: dict[str, int]

    @staticmethod
    def read(extracted: Path) -> ModelDescription:
        root = ElementTree.parse(extracted / "modelDescription.xml").getroot()
        return ModelDescription(
            model_identifier=root.find("CoSimulation").get("modelIdentifier"),
            instantiation_token=root.get("instantiationToken"),
            float64_references={
                variable.get("name"): int(variable.get("valueReference"))
                for variable in root.find("ModelVariables").findall("Float64")
            },
        )

    def binary(self, extracted: Path) -> Path:
        """The shared library this platform loads out of the FMU."""
        machine = platform.machine()
        machine = _MACHINES.get(machine, machine)
        system, suffix = _SYSTEMS[platform.system()]
        return (
            extracted / "binaries" / f"{machine}-{system}"
            / f"{self.model_identifier}{suffix}"
        )


def _log_to_stderr(environment, status, category, message) -> None:
    """The FMU's logger. stdout is the step protocol, so it cannot go there."""
    print(f"fmu: {message.decode(errors='replace')}", file=sys.stderr)


class CoSimulation:
    """One instantiated FMU, driven through its co-simulation entry points.

    The instance owns its internal state and is given a communication point
    and a step size, which is the contract the kernel already offers a process
    participant — this class is the translation between the two, and nothing
    above it deals in ctypes.
    """

    def __init__(self, binary: Path, description: ModelDescription):
        self._library = ctypes.CDLL(str(binary))
        for name, (restype, argtypes) in _SIGNATURES.items():
            entry_point = getattr(self._library, name)
            entry_point.restype = restype
            entry_point.argtypes = argtypes
        # The FMU calls this for the life of the instance, so the ctypes
        # trampoline has to outlive this constructor.
        self._logger = _LOG_CALLBACK(_log_to_stderr)
        self._instance = self._library.fmi3InstantiateCoSimulation(
            description.model_identifier.encode(),
            description.instantiation_token.encode(),
            None,
            False,  # visible
            True,   # loggingOn
            False,  # eventModeUsed
            False,  # earlyReturnAllowed
            None, 0,
            None, self._logger, None,
        )
        if not self._instance:
            raise ParticipantFailure(
                f"fmi3InstantiateCoSimulation returned no instance for "
                f"{description.model_identifier!r}"
            )

    def initialize(self) -> None:
        """Enter and leave initialization mode, leaving the FMU steppable.

        No stop time is declared: the Manifest's duration is the kernel's, and
        an FMU told a stop time it never reaches would reject the last step.
        """
        self._call(
            "fmi3EnterInitializationMode",
            self._library.fmi3EnterInitializationMode(
                self._instance, False, 0.0, 0.0, False, 0.0
            ),
        )
        self._call(
            "fmi3ExitInitializationMode",
            self._library.fmi3ExitInitializationMode(self._instance),
        )

    def set_float64(self, references, values) -> None:
        self._call(
            "fmi3SetFloat64",
            self._library.fmi3SetFloat64(
                self._instance, references, len(references),
                values, len(values),
            ),
        )

    def get_float64(self, references, values) -> None:
        self._call(
            "fmi3GetFloat64",
            self._library.fmi3GetFloat64(
                self._instance, references, len(references),
                values, len(values),
            ),
        )

    def do_step(self, communication_point: float, step_size: float) -> None:
        event_needed = ctypes.c_bool()
        terminate = ctypes.c_bool()
        early_return = ctypes.c_bool()
        last_successful_time = ctypes.c_double()
        self._call(
            "fmi3DoStep",
            self._library.fmi3DoStep(
                self._instance, communication_point, step_size, True,
                ctypes.byref(event_needed), ctypes.byref(terminate),
                ctypes.byref(early_return), ctypes.byref(last_successful_time),
            ),
        )

    def close(self) -> None:
        if self._instance is None:
            return
        instance, self._instance = self._instance, None
        self._call("fmi3Terminate", self._library.fmi3Terminate(instance))
        self._library.fmi3FreeInstance(instance)

    @staticmethod
    def _call(name: str, status: int) -> None:
        if status >= _FMI_FIRST_FAILING_STATUS:
            raise ParticipantFailure(f"{name} returned {_FMI_STATUS_NAMES[status]}")


class _Binding:
    """One Channel's schema fields bound to FMU variables of the same names.

    The value references and the value buffer are built once, at
    initialization, because they are the same on every step.
    """

    def __init__(self, field_names: list[str], references: dict[str, int]):
        self._field_names = field_names
        self._references = (ctypes.c_uint32 * len(field_names))(
            *(references[name] for name in field_names)
        )
        self._values = (ctypes.c_double * len(field_names))()

    def write(self, fmu: CoSimulation, fields: dict) -> None:
        self._values[:] = [fields[name] for name in self._field_names]
        fmu.set_float64(self._references, self._values)

    def read(self, fmu: CoSimulation) -> dict:
        fmu.get_float64(self._references, self._values)
        return dict(zip(self._field_names, self._values))


class FmuParticipant(StepParticipant):
    """A process participant whose behavior is an imported FMU's."""

    def __init__(self, fmu_path: Path):
        self._fmu_path = fmu_path
        self._extraction = None
        self._fmu = None
        self._inputs: dict[str, _Binding] = {}
        self._outputs: dict[str, _Binding] = {}

    def on_init(self, init: dict) -> None:
        self._extraction = tempfile.TemporaryDirectory(prefix="sil-fmu-")
        extracted = Path(self._extraction.name)
        with zipfile.ZipFile(self._fmu_path) as archive:
            archive.extractall(extracted)
        description = ModelDescription.read(extracted)
        self._bind_channels(init, description.float64_references)
        self._fmu = CoSimulation(description.binary(extracted), description)
        self._fmu.initialize()

    def _bind_channels(self, init: dict, references: dict[str, int]) -> None:
        """Split the declared Channels by the direction the init line gives.

        An input-direction Channel is written into the FMU before its step; an
        output-direction Channel is published from it after. The schema field
        names name the FMU variables on both sides.
        """
        types = schema.load(init["schemas"])
        for channel, declaration in init["channels"].items():
            binding = _Binding(
                types[declaration["schema"]].field_names, references
            )
            bindings = (
                self._inputs if declaration["direction"] == "in" else self._outputs
            )
            bindings[channel] = binding

    def on_step(self, t: int, dt: int, inputs: list):
        # Inputs arrive in publish order, so writing each in turn leaves the
        # newest Message on a Channel as the value the step sees.
        for message in inputs:
            binding = self._inputs.get(message.channel)
            if binding is not None:
                binding.write(self._fmu, message.data)
        self._fmu.do_step(t / NS_PER_S, dt / NS_PER_S)
        return [
            (channel, binding.read(self._fmu))
            for channel, binding in self._outputs.items()
        ]

    def close(self) -> None:
        """Terminate and free the instance, and drop the extracted FMU."""
        if self._fmu is not None:
            self._fmu.close()
        if self._extraction is not None:
            self._extraction.cleanup()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python -m sil.fmi <path/to/model.fmu>")
    participant = FmuParticipant(Path(args[0]))
    try:
        run(participant)
    finally:
        participant.close()


if __name__ == "__main__":
    main()
