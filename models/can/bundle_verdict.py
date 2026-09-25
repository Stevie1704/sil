"""Compare the installed-SiL and independent traces and keep the verdict.

    python models/can/bundle_verdict.py build/can/bundle

Fails unless both paths ran the same archive, SiL came from an installed
distribution rather than a source tree, and both traces are equal. The check
uses the distribution's own file record, not an installation path, because
the container layout is not a supported interface (SUPPORT.md). Writes
`evidence.json` next to the inputs. Standard library only.
"""

import json
import sys
from pathlib import Path


def verdict(directory):
    sil = json.loads((directory / "sil/trace.json").read_text())
    independent = json.loads((directory / "independent.json").read_text())
    failures = []
    equal = sil["trace"] == independent["trace"]
    if not sil["sil_installed"]:
        failures.append(
            f"sil was imported from {sil['sil_module']}, not an installed distribution"
        )
    if sil["fmu_sha256"] != independent["fmu_sha256"]:
        failures.append("the two paths ran different archives")
    if not sil["repeat_identical"]:
        failures.append("repeated Runs of one Manifest differ")
    if not equal:
        failures.append("installed SiL and the independent FMI path disagree")
    return {
        "fmu_sha256": sil["fmu_sha256"],
        "runtime_image": (directory / "runtime-image.txt").read_text().strip(),
        "qualification_image": (
            directory / "qualification-image.txt"
        ).read_text().strip(),
        "installed_sil": {key: sil[key] for key in (
            "execution_path", "sil_version", "sil_module", "sil_installed",
            "runner_build_info",
            "manifest_sha256", "recording_sha256", "repeat_identical", "deadlines",
        )},
        "independent": {key: independent[key] for key in (
            "execution_path", "reference_tool",
        )},
        "trace_rows": len(sil["trace"]),
        "traces_equal": equal,
        "failures": failures,
    }


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    result = verdict(directory)
    (directory / "evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    if result["failures"]:
        raise SystemExit("\n".join(result["failures"]))
    print(f"bundle validation passed: {result['trace_rows']} equal trace rows")
