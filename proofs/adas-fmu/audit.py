"""Audit the ADAS reference FMU's interface, and what SiL's inspection says.

Three static audits of the archive, none of which loads its binary:

- `interface_findings`: the archive holds exactly the expected members, and
  the description declares exactly the profile's variables. Each Schema
  field of `examples/adas-reference/schemas.json` is one variable named
  `<channel>.<field>` of the matching FMI 3.0 type, a `count` is a literal
  `<Dimension start>`, and the capabilities declare one fixed 10 ms step
  and nothing optional. It reads the archive with the standard library.
- FMPy's own validation of the description (in the preparation image).
- `inspection`: SiL's `sil-fmi-inspect` on the archive, and on
  `recorded-input.mapping.json`, the mapping a Run that replays the
  maneuvers' Recordings into this FMU would declare. Each binding is also
  inspected on its own. Until the importer maps these types and arrays,
  every binding must be refused for its type or its dimensions, and for
  nothing else.

    python proofs/adas-fmu/audit.py AdasReference.fmu OUT_DIR

writes `OUT_DIR/interface-audit.json` and `OUT_DIR/inspection.json`, and
exits 1 when a finding is not as expected.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from sil.fmi import library_suffix, platform_directory
from sil.fmi.inspection import inspect

PROOF_DIR = Path(__file__).resolve().parent
ROOT = PROOF_DIR.parents[1]
SCHEMAS = json.loads(
    (ROOT / "examples" / "adas-reference" / "schemas.json").read_text())
MAPPING = json.loads((PROOF_DIR / "recorded-input.mapping.json").read_text())
MODEL_IDENTIFIER = "AdasReference"

# The FMI 3.0 type that carries each Schema field type.
FMI_TYPES = {"f32": "Float32", "i32": "Int32", "i64": "Int64", "u8": "UInt8",
             "u32": "UInt32", "u64": "UInt64"}
# The unit of a Float field, from its name's suffix; other fields have none.
UNITS = (("_mps2", "m/s2"), ("_mps", "m/s"), ("_m", "m"))
PARAMETERS = {"hazard_acceleration_mps2": "-3", "max_change_mps2": "0.5"}
CO_SIMULATION = {
    "modelIdentifier": MODEL_IDENTIFIER,
    "canHandleVariableCommunicationStepSize": "false",
    "fixedInternalStepSize": "0.01",
    "hasEventMode": "false",
    "canGetAndSetFMUState": "false",
    "canSerializeFMUState": "false",
    "providesDirectionalDerivatives": "false",
    "providesAdjointDerivatives": "false",
    "providesPerElementDependencies": "false",
    "needsExecutionTool": "false",
    "canBeInstantiatedOnlyOncePerProcess": "false",
    "providesIntermediateUpdate": "false",
    "canReturnEarlyAfterIntermediateUpdate": "false",
    "providesEvaluateDiscreteStates": "false",
}
# What a binding is refused for while the importer lacks the type or shape.
MISSING = re.compile(r"which (is a (\w+) variable; this importer maps Binary "
                     r"and the scalar types .*|declares dimensions of 8 "
                     r"values; this importer maps variables of one value)$")


def _unit(field: str) -> str | None:
    return next((unit for suffix, unit in UNITS if field.endswith(suffix)),
                None)


def expected_variables() -> dict[str, dict]:
    """Every variable the profile requires, as the description declares it."""
    variables = {
        "time": {"type": "Float64", "causality": "independent",
                 "variability": "continuous", "unit": "s", "dimensions": []},
        **{name: {"type": "Float64", "causality": "parameter",
                  "variability": "fixed", "unit": "m/s2", "dimensions": [],
                  "start": start}
           for name, start in PARAMETERS.items()},
    }
    for channel, spec in MAPPING["channels"].items():
        causality = "input" if spec["direction"] == "in" else "output"
        for field in SCHEMAS[spec["schema"]]["fields"]:
            fmi_type = FMI_TYPES[field["type"]]
            variables[f"{channel}.{field['name']}"] = {
                "type": fmi_type, "causality": causality,
                "variability": "discrete",
                "unit": _unit(field["name"]) if fmi_type == "Float32"
                else None,
                "dimensions": [field["count"]] if "count" in field else [],
            }
    return variables


def expected_members() -> set[str]:
    return {"modelDescription.xml",
            f"binaries/{platform_directory()}/{MODEL_IDENTIFIER}"
            f"{library_suffix()}",
            "documentation/identity.json",
            "documentation/licenses/LICENSE-SiL.txt",
            "documentation/licenses/LICENSE-FMI.txt"}


def _declared(element: ElementTree.Element) -> dict:
    return {
        "type": element.tag,
        "causality": element.get("causality"),
        "variability": element.get("variability"),
        "unit": element.get("unit"),
        "dimensions": [int(d.get("start")) for d in element.findall(
            "Dimension")],
        "value_reference": int(element.get("valueReference")),
        "initial": element.get("initial"),
        "start": element.get("start"),
    }


def read_interface(fmu: Path) -> dict:
    """What the archive holds and its description declares."""
    with zipfile.ZipFile(fmu) as archive:
        members = sorted(archive.namelist())
        root = ElementTree.fromstring(archive.read("modelDescription.xml"))
    co_simulation = root.find("CoSimulation")
    experiment = root.find("DefaultExperiment")
    return {
        "members": members,
        "model": {key: root.get(key) for key in (
            "fmiVersion", "modelName", "version", "generationTool",
            "license", "instantiationToken")},
        "interfaces": [tag for tag in ("CoSimulation", "ModelExchange",
                                       "ScheduledExecution")
                       if root.find(tag) is not None],
        "co_simulation": dict(co_simulation.attrib)
        if co_simulation is not None else {},
        "default_experiment": dict(experiment.attrib)
        if experiment is not None else {},
        "units": [unit.get("name") for unit in root.iter("Unit")],
        "variables": {element.get("name"): _declared(element)
                      for element in root.find("ModelVariables")},
        "outputs": [int(o.get("valueReference")) for o in root.iter("Output")],
        "initial_unknowns": [int(u.get("valueReference"))
                             for u in root.iter("InitialUnknown")],
    }


def interface_findings(interface: dict) -> list[str]:
    """Every way the archive differs from the profile's interface."""
    findings = []
    if set(interface["members"]) != expected_members():
        findings.append(f"members {interface['members']}")
    if interface["model"]["fmiVersion"] != "3.0":
        findings.append(f"fmiVersion {interface['model']['fmiVersion']}")
    if interface["interfaces"] != ["CoSimulation"]:
        findings.append(f"interfaces {interface['interfaces']}")
    if interface["co_simulation"] != CO_SIMULATION:
        findings.append(f"CoSimulation {interface['co_simulation']}")
    if interface["default_experiment"].get("stepSize") != "0.01":
        findings.append(f"DefaultExperiment {interface['default_experiment']}")
    declared = interface["variables"]
    expected = expected_variables()
    if set(declared) != set(expected):
        findings.append(
            f"variables missing {sorted(set(expected) - set(declared))}, "
            f"unexpected {sorted(set(declared) - set(expected))}")
    for name in sorted(set(declared) & set(expected)):
        for key, value in expected[name].items():
            if declared[name][key] != value:
                findings.append(f"{name}: {key} is {declared[name][key]!r}, "
                                f"expected {value!r}")
    references = [v["value_reference"] for v in declared.values()]
    if len(set(references)) != len(references):
        findings.append("value references repeat")
    outputs = sorted(v["value_reference"] for v in declared.values()
                     if v["causality"] == "output")
    if interface["outputs"] != outputs:
        findings.append(f"ModelStructure outputs {interface['outputs']}")
    if any(v["causality"] == "output" and v["initial"] != "exact"
           for v in declared.values()) or interface["initial_unknowns"]:
        findings.append("an output is not initial=exact")
    return findings


def _probe(fmu: Path, bind: str) -> str | None:
    """The rejection of one binding, inspected with its own Channel only."""
    channel = bind.partition(":")[0]
    mapping = {"sil_fmi_mapping": 1, "schemas": MAPPING["schemas"],
               "channels": {channel: MAPPING["channels"][channel]},
               "bind": [bind]}
    return inspect(fmu, mapping)["mapping"]["rejection"]


def inspection(fmu: Path) -> dict:
    """SiL's inspection of the archive and of the recorded-input mapping."""
    report = inspect(fmu, MAPPING)
    probes = {bind: _probe(fmu, bind) for bind in MAPPING["bind"]}
    missing_types = sorted({m[2] for r in probes.values()
                            if r and (m := MISSING.search(r)) and m[2]})
    return {
        "verdict": report["verdict"],
        "unusable": report["unusable"],
        "mapping": report["mapping"],
        "bindings": probes,
        "missing_types": missing_types,
        "arrays_refused": sorted(
            bind for bind, r in probes.items()
            if r and "declares dimensions" in r),
    }


def inspection_findings(result: dict) -> list[str]:
    """Every way the inspection differs from "refused only for types and
    arrays"."""
    findings = []
    if result["verdict"] != "mapping-rejected":
        findings.append(f"verdict {result['verdict']}")
    if result["unusable"]:
        findings.append(f"unusable {result['unusable']}")
    for bind, rejection in result["bindings"].items():
        if rejection is None or not MISSING.search(rejection):
            findings.append(f"{bind}: {rejection}")
    return findings


def fmpy_problems(fmu: Path) -> list[str]:
    # FMPy is in the preparation image only, not in the repository's tests.
    from fmpy.validation import validate_fmu
    return validate_fmu(str(fmu))


def audit(fmu: Path, out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    interface = read_interface(fmu)
    problems = fmpy_problems(fmu)
    (out_dir / "interface-audit.json").write_text(json.dumps(
        {"interface": interface, "fmpy_validation": problems}, indent=2)
        + "\n")
    inspected = inspection(fmu)
    (out_dir / "inspection.json").write_text(
        json.dumps(inspected, indent=2) + "\n")
    return ([f"FMPy: {p}" for p in problems] + interface_findings(interface)
            + inspection_findings(inspected))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fmu", type=Path, help="the FMU archive to audit")
    parser.add_argument("out_dir", type=Path,
                        help="where the audit documents go")
    args = parser.parse_args()
    findings = audit(args.fmu, args.out_dir)
    for finding in findings:
        print(finding)
    sys.exit(1 if findings else 0)
