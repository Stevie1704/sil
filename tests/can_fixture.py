"""The acceptance fixture's CAN FMUs, as archives a test can drive.

The two demo FMUs are built by `proofs/fmi-ls-bus/run-proof.sh` inside a pinned
container and are not vendored here, so what a test packages is their own
*declarations* — read back from `proofs/fmi-ls-bus/evidence/profile.json`, the
fixture's record of what it inspected — around whichever binary the test wants
behind them. A fixture that is rebuilt and changes takes these tests with it.

`fmu_archive()` packages either FMU either way:

- the declarations alone, which is all a mapping needs — binding happens before
  the FMU is loaded, so the absent binary is what a resolved mapping runs into;
- the declarations around a `binary=` a test built, which drives the FMU's
  lifecycle without docker.

Three files carry the declarations, and all three are packaged, because the
group importer reads all three: `modelDescription.xml` for the variables,
`terminalsAndIcons/terminalsAndIcons.xml` for the terminals a connection names,
and the FMI-LS-BUS layered-standard manifest for the profile and for which of
two connected FMUs models the bus.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

from conftest import ROOT

FIXTURE_PROFILE = ROOT / "proofs" / "fmi-ls-bus" / "evidence" / "profile.json"
CAN_NODE = "DemoCanNodeTriggeredOutput.fmu"
CAN_BUS = "DemoCanBusSimulation.fmu"

# Both FMUs' Binary variables declare `maxSize` 2048, so a Channel that can
# carry any payload they are allowed to produce carries 2048 bytes.
CAN_BUFFER_BYTES = 2048

# The node's one terminal, and its four members.
_NODE_TERMINAL = "CanChannel"
RX_DATA = f"{_NODE_TERMINAL}.Rx_Data"
TX_DATA = f"{_NODE_TERMINAL}.Tx_Data"
RX_CLOCK = f"{_NODE_TERMINAL}.Rx_Clock"
TX_CLOCK = f"{_NODE_TERMINAL}.Tx_Clock"

# The media type both FMUs' bus buffers declare, without its parameters.
CAN_PROFILE = "application/org.fmi-standard.fmi-ls-bus.can"

_TERMINALS_MEMBER = "terminalsAndIcons/terminalsAndIcons.xml"
_BUS_MANIFEST_MEMBER = (
    "extra/org.fmi-standard.fmi-ls-bus/fmi-ls-manifest.xml"
)


def _described(fmu: str) -> dict:
    """What the fixture recorded about one FMU of the acceptance profile."""
    return json.loads(FIXTURE_PROFILE.read_text())["fmus"][fmu]


def _variable_element(variable: dict) -> str:
    """One variable of the fixture's profile, as the description declares it."""
    kind = variable["type"]
    attributes = " ".join(
        f"{name}={quoteattr(value)}"
        for name, value in variable.items()
        if name not in ("type", "dimension_start")
    )
    dimension = (
        f'<Dimension start={quoteattr(variable["dimension_start"])}/>'
        if "dimension_start" in variable else ""
    )
    return f"<{kind} {attributes}>{dimension}</{kind}>"


def description(fmu: str = CAN_NODE, model_identifier: str | None = None) -> str:
    """`modelDescription.xml` for one FMU, as the fixture recorded it."""
    declaration = _described(fmu)["modelDescription"]
    capabilities = " ".join(
        f"{name}={quoteattr(value)}"
        for name, value in sorted(declaration["capabilities"].items())
    )
    variables = "".join(
        _variable_element(variable) for variable in declaration["variables"]
    )
    identifier = model_identifier or declaration["modelIdentifier"]
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<fmiModelDescription '
        f'fmiVersion={quoteattr(declaration["fmiVersion"])} '
        f'instantiationToken={quoteattr(declaration["instantiationToken"])}>'
        f'<CoSimulation modelIdentifier={quoteattr(identifier)} '
        f'{capabilities}/>'
        f'<ModelVariables>{variables}</ModelVariables>'
        f'</fmiModelDescription>'
    )


def terminals(fmu: str = CAN_NODE) -> str:
    """`terminalsAndIcons.xml` for one FMU, as the fixture recorded it."""
    declared = "".join(
        f'<Terminal name={quoteattr(terminal["name"])} '
        f'terminalKind={quoteattr(terminal["terminalKind"])} '
        f'matchingRule={quoteattr(terminal["matchingRule"])}>'
        + "".join(
            f'<TerminalMemberVariable '
            f'variableKind={quoteattr(member["variableKind"])} '
            f'variableName={quoteattr(member["variableName"])} '
            f'memberName={quoteattr(member["memberName"])}/>'
            for member in terminal["members"]
        )
        + '</Terminal>'
        for terminal in _described(fmu)["terminals"]
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<fmiTerminalsAndIcons fmiVersion="3.0">'
        f'<Terminals>{declared}</Terminals>'
        f'</fmiTerminalsAndIcons>'
    )


def bus_manifest(fmu: str = CAN_NODE) -> str:
    """The FMI-LS-BUS layered-standard manifest, as the fixture recorded it."""
    declared = _described(fmu)["layeredStandardManifest"]
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<fmiLayeredStandardManifest '
        f'xmlns:fmi-ls="http://fmi-standard.org/fmi-ls-manifest" '
        f'fmi-ls:fmi-ls-name={quoteattr(declared["name"])} '
        f'fmi-ls:fmi-ls-version={quoteattr(declared["version"])} '
        f'isBusSimulationFMU={quoteattr(declared["isBusSimulationFMU"])}/>'
    )


def fmu_archive(
    path: Path,
    fmu: str = CAN_NODE,
    *,
    model_identifier: str | None = None,
    binary: Path | None = None,
    platform_directory: str | None = None,
    rewrite=lambda text: text,
    rewrite_terminals=lambda text: text,
    rewrite_manifest=lambda text: text,
) -> Path:
    """One FMU's three declaration files, with `binary` behind them, if any.

    A rewrite that answers None leaves its member out of the archive, which is
    how an FMU that declares no terminals or no layered standard is packaged.
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "modelDescription.xml", rewrite(description(fmu, model_identifier))
        )
        for member, rewritten in (
            (_TERMINALS_MEMBER, rewrite_terminals(terminals(fmu))),
            (_BUS_MANIFEST_MEMBER, rewrite_manifest(bus_manifest(fmu))),
        ):
            if rewritten is not None:
                archive.writestr(member, rewritten)
        if binary is not None:
            archive.write(
                binary,
                arcname=f"binaries/{platform_directory}/{binary.name}",
            )
    return path


def node_fmu(path: Path, **packaging) -> Path:
    """The CAN node's declarations, packaged."""
    return fmu_archive(path, CAN_NODE, **packaging)


def bus_fmu(path: Path, **packaging) -> Path:
    """The CAN bus simulation FMU's declarations, packaged."""
    return fmu_archive(path, CAN_BUS, **packaging)
