"""The `AccController` archive's identity and declared interface (#194).

The archive digest depends on the SiL revision it was exported at: the
exporter embeds that revision in `resources/identity.json` and derives the
instantiation token from it. A bundle regenerated at another revision is
therefore a different archive of the same model. What stays fixed is the
model source the archive carries and the interface it declares, so those are
what this module states for the acceptance to compare:

- `archive_identity`: the archive digest, the embedded identity, and the
  digests of the model source and the shared control law in `resources/`.
  An archive whose files are not the ones its identity names is refused.
- `inspected_interface` and `audited_interface`: the declared interface as
  `sil-fmi-inspect` reports it and as the #178 audit recorded it, in one
  shape, without the revision-dependent instantiation token.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

MODEL_SOURCE = "resources/controller.py"
CONTROL_LAW = "resources/dynamics.py"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive_identity(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as opened:
        embedded = json.loads(opened.read("resources/identity.json"))
        files = {"model_sha256": _sha256(opened.read(MODEL_SOURCE)),
                 "dynamics_sha256": _sha256(opened.read(CONTROL_LAW))}
    for key, member in (("model_sha256", MODEL_SOURCE),
                        ("dynamics_sha256", CONTROL_LAW)):
        if files[key] != embedded[key]:
            raise ValueError(f"{member} is {files[key]}; the archive's "
                             f"identity names {embedded[key]}")
    return {"archive_sha256": _sha256(Path(archive).read_bytes()),
            "embedded": embedded, **files}


def _variable(name, kind, causality, unit, start, dimensions) -> dict:
    return {"name": name, "type": kind, "causality": causality, "unit": unit,
            "start": start, "dimensions": dimensions}


def inspected_interface(report: dict) -> dict:
    """The interface in a `sil-fmi-inspect` report."""
    facts = report["facts"]
    return {
        "fmi_version": facts["fmi_version"],
        "model_name": facts["model_name"],
        "generation_tool": facts["generation_tool"],
        "interfaces": sorted(facts["interfaces"]),
        "co_simulation": facts["interfaces"]["CoSimulation"],
        "platforms": facts["platforms"],
        "variables": [_variable(v["name"], v["type"], v["causality"], v["unit"],
                                v["start"], len(v["dimensions"]))
                      for v in report["variables"]],
    }


def audited_interface(audit: dict) -> dict:
    """The interface in one entry of #178's `fmu-audit.json`."""
    return {
        "fmi_version": audit["fmi_version"],
        "model_name": audit["model_name"],
        "generation_tool": audit["generation_tool"],
        "interfaces": sorted(audit["interfaces"]),
        "co_simulation": audit["capabilities"],
        "platforms": audit["platforms"],
        "variables": [_variable(v["name"], v["type"], v["causality"], v["unit"],
                                v["start"], v["dimensions"])
                      for v in audit["variables"]],
    }
