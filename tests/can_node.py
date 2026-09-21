"""The acceptance fixture's CAN node, as an FMU archive a test can drive.

The node itself is built by `proofs/fmi-ls-bus/run-proof.sh` inside a pinned
container and is not vendored here, so what a test packages is the node's own
*declarations* — read back from `proofs/fmi-ls-bus/evidence/profile.json`, the
fixture's record of what it inspected — around whichever binary the test wants
behind them. A fixture that is rebuilt and changes takes these tests with it.

`node_fmu()` packages it either way:

- the description alone, which is all a mapping needs — binding happens before
  the FMU is loaded, so the absent binary is what a resolved mapping runs into;
- the description around a `binary=` a test built, which drives the node's
  lifecycle without docker.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

from conftest import ROOT

FIXTURE_PROFILE = ROOT / "proofs" / "fmi-ls-bus" / "evidence" / "profile.json"
CAN_NODE = "DemoCanNodeTriggeredOutput.fmu"

# The node's Binary variables declare `maxSize` 2048, so a Channel that can
# carry any payload it is allowed to produce carries 2048 bytes.
CAN_BUFFER_BYTES = 2048

RX_DATA = "CanChannel.Rx_Data"
TX_DATA = "CanChannel.Tx_Data"
RX_CLOCK = "CanChannel.Rx_Clock"
TX_CLOCK = "CanChannel.Tx_Clock"


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


def description(model_identifier: str | None = None) -> str:
    """`modelDescription.xml` for the node, as the fixture recorded it."""
    described = json.loads(FIXTURE_PROFILE.read_text())["fmus"][CAN_NODE]
    declaration = described["modelDescription"]
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


def node_fmu(
    path: Path,
    *,
    model_identifier: str | None = None,
    binary: Path | None = None,
    platform_directory: str | None = None,
    rewrite=lambda text: text,
) -> Path:
    """The node's description, packaged with `binary` behind it, if any."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "modelDescription.xml", rewrite(description(model_identifier))
        )
        if binary is not None:
            archive.write(
                binary,
                arcname=f"binaries/{platform_directory}/{binary.name}",
            )
    return path
