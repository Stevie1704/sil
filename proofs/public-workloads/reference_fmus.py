"""Audit the Modelica Reference FMUs against the profile SiL has qualified.

The qualified profile is the one the ACC evidence establishes
(`proofs/acc-fmi/INSTALL.md`): FMI 3.0 Co-Simulation, a Linux x86-64
binary, and scalar Float64 inputs and outputs. An archive outside it is
reported with every reason, not adapted. An archive inside it is simulated
once by FMPy from its default start, for at most `SMOKE_HORIZON_S`, as an
importer smoke test; that is not a SiL result.
"""
import math
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def profile_gaps(root, platforms):
    """Every reason one model description falls outside the qualified profile."""
    if root.get("fmiVersion") != "3.0":
        # FMI 2.0 names types and platforms differently; one reason is exact.
        return [f"fmiVersion {root.get('fmiVersion')}: the Importer drives FMI 3.0 only"]
    gaps = []
    if root.find("CoSimulation") is None:
        gaps.append("no Co-Simulation interface")
    if "x86_64-linux" not in platforms:
        gaps.append("no x86_64-linux binary")
    variables = root.find("ModelVariables")
    for variable in [] if variables is None else variables:
        causality = variable.get("causality", "local")
        if causality not in ("input", "output"):
            continue
        if variable.tag != "Float64":
            gaps.append(f"{causality} {variable.get('name')} is {variable.tag}")
        elif variable.findall("Dimension"):
            gaps.append(f"{causality} {variable.get('name')} is an array")
    return gaps


# A smoke test, not the full default experiment: Roberts' runs to 1e8 s on a
# 1e-3 s fixed internal step, which no importer check needs.
SMOKE_HORIZON_S = 10.0


def _simulate(archive):
    from fmpy import read_model_description, simulate_fmu
    experiment = read_model_description(str(archive)).defaultExperiment
    start = float(experiment.startTime or 0.0)
    default_stop = float(experiment.stopTime or start + 1.0)
    stop = min(default_stop, start + SMOKE_HORIZON_S)
    result = simulate_fmu(str(archive), fmi_type="CoSimulation", start_time=start, stop_time=stop)
    finite = all(math.isfinite(float(value)) for row in result for value in row)
    return {"start_s": start, "stop_s": stop, "default_stop_s": default_stop,
            "rows": len(result), "columns": list(result.dtype.names), "finite": finite}


def audit(zip_path):
    report = {}
    with zipfile.ZipFile(zip_path) as bundle, tempfile.TemporaryDirectory() as directory:
        for member in sorted(n for n in bundle.namelist() if n.endswith(".fmu")):
            archive = Path(directory) / member
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(bundle.read(member))
            with zipfile.ZipFile(archive) as fmu:
                root = ElementTree.fromstring(fmu.read("modelDescription.xml"))
                platforms = sorted({Path(n).parts[1] for n in fmu.namelist()
                                    if n.startswith("binaries/") and len(Path(n).parts) > 2})
            gaps = profile_gaps(root, platforms)
            entry = {"fmi_version": root.get("fmiVersion"), "platforms": platforms,
                     "inside_qualified_profile": not gaps, "profile_gaps": gaps}
            if not gaps:
                entry["fmpy_smoke"] = _simulate(archive)
            report[member] = entry
    return report
