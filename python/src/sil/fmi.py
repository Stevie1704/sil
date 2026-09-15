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
from sil.participant import (
    ManifestError,
    ParticipantFailure,
    StepParticipant,
    run,
)

# Virtual time is integer nanoseconds; seconds are derived from those integers
# on every step and never accumulated, so the same integer always produces the
# same double. An integer denominator keeps that true past the 53 bits an
# int-to-float conversion holds exactly.
NS_PER_S = 1_000_000_000

# The one FMI version this importer drives. Anything else is rejected rather
# than half-driven.
_FMI_VERSION = "3.0"

# fmi3Status. Only OK means the call succeeded; every other status aborts the
# Run before the importer can continue with a possibly invalid FMU state.
_FMI_STATUS_NAMES = ("OK", "Warning", "Discard", "Error", "Fatal")
_FMI_SUCCESS_STATUS = 0
_FMI_FATAL_STATUS = 4

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

# The co-simulation entry points this importer drives, with their argument and
# return types.
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


def platform_directory() -> str:
    """The `binaries/` subdirectory this platform's shared library lives in."""
    machine = platform.machine()
    machine = _MACHINES.get(machine, machine)
    system, _ = _SYSTEMS[platform.system()]
    return f"{machine}-{system}"


def library_suffix() -> str:
    """The shared-library suffix this platform's FMU binary carries."""
    _, suffix = _SYSTEMS[platform.system()]
    return suffix


def _by_causality(variables, causality: str) -> dict[str, int]:
    """The Float64 variables of one causality, by name.

    Only `input` and `output` variables take part in the Channel mapping. A
    parameter, a local, or the independent variable `time` is the FMU's own
    business and no Channel names it.
    """
    return {
        variable.get("name"): int(variable.get("valueReference"))
        for variable in variables
        if variable.get("causality") == causality
    }


@dataclass(frozen=True)
class ModelDescription:
    """What `modelDescription.xml` says that driving the FMU depends on."""

    model_identifier: str
    instantiation_token: str
    inputs: dict[str, int]
    outputs: dict[str, int]

    @staticmethod
    def read(extracted: Path) -> ModelDescription:
        """Parse the description, rejecting an FMU this importer cannot drive.

        Both rejections are eager and specific: an FMI 2.0 export otherwise
        loads and fails on a missing symbol, and a Model Exchange FMU
        otherwise fails on an absent element.
        """
        try:
            root = ElementTree.parse(
                extracted / "modelDescription.xml"
            ).getroot()
        except (OSError, ElementTree.ParseError) as error:
            raise ManifestError(
                f"FMU has no readable modelDescription.xml: {error}"
            ) from error
        version = root.get("fmiVersion")
        if version != _FMI_VERSION:
            raise ManifestError(
                f"FMU declares fmiVersion {version!r}; this importer drives "
                f"FMI {_FMI_VERSION} co-simulation only"
            )
        co_simulation = root.find("CoSimulation")
        if co_simulation is None:
            raise ManifestError(
                "FMU declares no co-simulation interface; this importer "
                "drives neither Model Exchange nor Scheduled Execution"
            )
        variables = root.find("ModelVariables").findall("Float64")
        return ModelDescription(
            model_identifier=co_simulation.get("modelIdentifier"),
            instantiation_token=root.get("instantiationToken"),
            inputs=_by_causality(variables, "input"),
            outputs=_by_causality(variables, "output"),
        )

    def binary(self, extracted: Path) -> Path:
        """The shared library this platform loads out of the FMU."""
        directory = platform_directory()
        binary = (
            extracted / "binaries" / directory
            / f"{self.model_identifier}{library_suffix()}"
        )
        if not binary.exists():
            raise ManifestError(
                f"FMU {self.model_identifier!r} carries no binary for "
                f"{directory}: binaries/{directory}/{binary.name} is not in "
                f"the archive"
            )
        return binary


def _load(binary: Path) -> ctypes.CDLL:
    """Open the FMU's shared library with the signatures ctypes cannot infer.

    An instance handle is a pointer: without a declared `c_void_p` restype
    ctypes would truncate it to an int.
    """
    library = ctypes.CDLL(str(binary))
    for name, (restype, argtypes) in _SIGNATURES.items():
        entry_point = getattr(library, name)
        entry_point.restype = restype
        entry_point.argtypes = argtypes
    return library


def _log_to_stderr(environment, status, category, message) -> None:
    """The FMU's logger. stdout is the step protocol, so it cannot go there."""
    print(f"fmu: {message.decode(errors='replace')}", file=sys.stderr)


def _status_name(status: int) -> str:
    """Name an FMI status without losing an unknown status to IndexError."""
    if 0 <= status < len(_FMI_STATUS_NAMES):
        return _FMI_STATUS_NAMES[status]
    return f"unknown status {status}"


class CoSimulation:
    """One instantiated FMU, driven through its co-simulation entry points.

    The instance owns its internal state and is given a communication point
    and a step size, which is the contract the kernel already offers a process
    participant — this class is the translation between the two, and nothing
    above it deals in ctypes.
    """

    def __init__(self, binary: Path, description: ModelDescription):
        self._library = _load(binary)
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
        self._call("fmi3EnterInitializationMode", False, 0.0, 0.0, False, 0.0)
        self._call("fmi3ExitInitializationMode")

    def set_float64(self, references, values) -> None:
        self._call(
            "fmi3SetFloat64", references, len(references), values, len(values)
        )

    def get_float64(self, references, values) -> None:
        self._call(
            "fmi3GetFloat64", references, len(references), values, len(values)
        )

    def do_step(self, communication_point: float, step_size: float) -> None:
        event_needed = ctypes.c_bool()
        terminate = ctypes.c_bool()
        early_return = ctypes.c_bool()
        last_successful_time = ctypes.c_double()
        self._call(
            "fmi3DoStep", communication_point, step_size, True,
            ctypes.byref(event_needed), ctypes.byref(terminate),
            ctypes.byref(early_return), ctypes.byref(last_successful_time),
        )
        if terminate.value:
            raise ParticipantFailure(
                "fmi3DoStep requested termination via terminateSimulation"
            )

    def close(self) -> None:
        """Terminate the instance and free it, even if terminating failed.

        The instance holds the FMU's memory either way, so the free is the
        half the failing path needs most.
        """
        if self._instance is None:
            return
        try:
            self._call("fmi3Terminate")
        finally:
            self._library.fmi3FreeInstance(self._instance)
            self._instance = None

    def _call(self, name: str, *arguments) -> None:
        """Invoke one co-simulation entry point on this instance.

        Every entry point takes the instance first and answers an fmi3Status,
        so naming it once here keeps the name in the diagnostic the same name
        that was called.
        """
        status = getattr(self._library, name)(self._instance, *arguments)
        if status == _FMI_SUCCESS_STATUS:
            return
        if status == _FMI_FATAL_STATUS:
            # FMI 3.0 allows no further call on an instance that answered
            # Fatal, terminating and freeing it included. Dropping the handle
            # here is what stops `close` from calling into a dead FMU; the
            # instance's memory goes when this process does.
            self._instance = None
        raise ParticipantFailure(f"{name} returned {_status_name(status)}")


class _ChannelBinding:
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


def _declarations(init: dict, types: dict) -> dict[str, dict[str, str]]:
    """Each declared schema field name, mapped to the Channel that declared it.

    Split by the direction the init line gives, because the direction decides
    which side of the step the FMU variable of that name is touched on.
    """
    declared: dict[str, dict[str, str]] = {"in": {}, "out": {}}
    for channel, declaration in init["channels"].items():
        declared[declaration["direction"]].update(
            dict.fromkeys(types[declaration["schema"]].field_names, channel)
        )
    return declared


def _require_total_match(
    declared: dict[str, str],
    variables: dict[str, int],
    causality: str,
    model_identifier: str,
) -> None:
    """Require every name on one side of the mapping to be on the other.

    `declared` maps each schema field name to the Channel that declared it, so
    both diagnostics can name the Channel and the FMU.
    """
    for name, channel in declared.items():
        if name not in variables:
            raise ManifestError(
                f"Channel {channel!r} declares schema field {name!r}, which is "
                f"no {causality} variable of FMU {model_identifier!r}"
            )
    searched = ", ".join(sorted(repr(c) for c in set(declared.values())))
    for name in variables:
        if name not in declared:
            raise ManifestError(
                f"FMU {model_identifier!r} declares {causality} variable "
                f"{name!r}, which is a schema field of no {causality}-direction "
                f"Channel (searched: {searched or 'none'})"
            )


class FmuParticipant(StepParticipant):
    """A process participant whose behavior is an imported FMU's."""

    def __init__(self, fmu_path: Path):
        self.name = ""  # the init line's, for the one diagnostic the kernel misses
        self._fmu_path = fmu_path
        self._extraction = None
        self._fmu = None
        self._inputs: dict[str, _ChannelBinding] = {}
        self._outputs: dict[str, _ChannelBinding] = {}

    def on_init(self, init: dict) -> None:
        self.name = init["name"]
        # The extracted archive belongs to the Run, not to the machine-wide
        # temporary directory. The process participant inherits the runner's
        # working directory, which is the Run's working directory here.
        self._extraction = tempfile.TemporaryDirectory(
            prefix="sil-fmu-", dir=Path.cwd()
        )
        extracted = Path(self._extraction.name)
        try:
            with zipfile.ZipFile(self._fmu_path) as archive:
                archive.extractall(extracted)
        except (OSError, zipfile.BadZipFile) as error:
            raise ManifestError(
                f"cannot read FMU {str(self._fmu_path)!r}: {error}"
            ) from error
        description = ModelDescription.read(extracted)
        self._bind_channels(init, description)
        self._fmu = CoSimulation(description.binary(extracted), description)
        self._fmu.initialize()

    def _bind_channels(self, init: dict, description: ModelDescription) -> None:
        """Split the declared Channels by the direction the init line gives.

        An input-direction Channel is written into the FMU before its step; an
        output-direction Channel is published from it after. The schema field
        names name the FMU variables on both sides, and the match is total in
        both directions — a name on one side and not the other is a mapping
        mistake, which is worth rejecting rather than carrying as a silently
        zero-valued variable.
        """
        types = schema.load(init["schemas"])
        declared = _declarations(init, types)
        _require_total_match(
            declared["in"], description.inputs, "input",
            description.model_identifier,
        )
        _require_total_match(
            declared["out"], description.outputs, "output",
            description.model_identifier,
        )
        for channel, declaration in init["channels"].items():
            incoming = declaration["direction"] == "in"
            bindings = self._inputs if incoming else self._outputs
            bindings[channel] = _ChannelBinding(
                types[declaration["schema"]].field_names,
                description.inputs if incoming else description.outputs,
            )

    def on_step(self, t: int, dt: int, inputs: list):
        # Inputs arrive in publish order, so writing each in turn leaves the
        # newest Message on a Channel as the value the step sees.
        for message in inputs:
            self._inputs[message.channel].write(self._fmu, message.data)
        self._fmu.do_step(t / NS_PER_S, dt / NS_PER_S)
        return [
            (channel, binding.read(self._fmu))
            for channel, binding in self._outputs.items()
        ]

    def close(self) -> None:
        """Terminate and free the instance, and drop the extracted FMU.

        The diagnostic stays unframed: on every path the kernel can see, the
        kernel is what names the participant.
        """
        fmu, extraction = self._fmu, self._extraction
        self._fmu = None
        self._extraction = None
        try:
            if fmu is not None:
                fmu.close()
        finally:
            if extraction is not None:
                extraction.cleanup()


def _close_after_failure(participant: FmuParticipant) -> None:
    """Drop the FMU on the way out of a failure that is already reported.

    Terminating an FMU that has already failed may fail in turn; that second
    diagnostic must not replace the first one.
    """
    try:
        participant.close()
    except ParticipantFailure:
        pass


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python -m sil.fmi <path/to/model.fmu>")
    participant = FmuParticipant(Path(args[0]))
    try:
        run(participant)
    except BaseException:
        _close_after_failure(participant)
        raise
    try:
        participant.close()
    except ParticipantFailure as error:
        # The step protocol is over by the time the FMU is terminated, so this
        # failure cannot travel as a `fail` line and the kernel sees only a
        # nonzero exit. Name the participant, the call and the status here.
        raise SystemExit(
            f"participant {participant.name!r} failed: {error}"
        ) from error


if __name__ == "__main__":
    main()
