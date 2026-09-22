"""Execute the ACC acceptance bundle from an installed SiL runtime (#151).

Runs inside the example image: the production runtime plus this consumer
material. No exporter, no independent importer, no toolchain and no source
tree are present. Every artifact this compares against was pinned by
``acceptance_prepare.py`` and is re-hashed before the first Run starts.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import sys
import sysconfig
from pathlib import Path

import acceptance_bundle
import scenarios
from acceptance_contract import (
    BUILD_TREE_ROOTS,
    FMU_DIR,
    FORBIDDEN_MODULES,
    FORBIDDEN_TOOLS,
    IMAGES_NAME,
    MODELS,
    REFERENCE_DIR,
    SENSITIVITY_CHECKS,
    scenario_checks,
    sensitivity_row,
)
from acceptance_verdict import (
    determinism_summary,
    envelope_verdict,
    expected_failure_verdict,
    require_minimal_runtime,
)
from proof_support import (PARTICIPANT_TIMEOUT_MS, RUNNER, file_sha256, require,
                           run_manifest_twice, single_receipt, write_json)
from sensitivity_compare import (compare_row, recording_observations,
                                 reference_observations, within_reference_tolerance)
from sensitivity_contract import REFERENCE_ABS_TOL
from sensitivity_contract import configuration as sensitivity_configuration
from sensitivity_manifest import manifest_for

KPI_MARKER = "ACC_SENSITIVITY_KPI"

def _reference(bundle: Path, kind: str, name: str) -> dict:
    return json.loads((bundle / REFERENCE_DIR / f"{kind}-{name}.json").read_text())


def _installed_sil() -> dict:
    """The importable SiL must be the installed distribution, not a checkout."""
    import sil

    module = Path(sil.__file__).resolve()
    require(
        module.parent.parent.name == "site-packages",
        f"SiL is imported from {module}, which is not an installed distribution",
    )
    source_paths = [
        entry for entry in sys.path
        if entry and Path(entry).name == "src" and (Path(entry) / "sil" / "manifest.py").exists()
    ]
    require(not source_paths, f"a SiL source tree is on the import path: {source_paths}")
    return {
        "module": str(module),
        "version": importlib.metadata.version("sil"),
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("mcap", "lz4", "zstandard")
        },
    }


def capture_runtime(bundle: Path, index: dict, out: Path) -> dict:
    """Bind this Run to the installed bundle it is executing from."""
    require(
        platform.system() == "Linux" and platform.machine() == "x86_64",
        "the acceptance bundle is qualified for Linux x86-64 only",
    )
    require(PARTICIPANT_TIMEOUT_MS > 0, "the response deadline must be positive")
    runner = shutil.which(RUNNER) or RUNNER
    require(Path(runner).is_file(), f"the installed runner {runner} is missing")
    require(
        not str(runner).startswith(BUILD_TREE_ROOTS),
        f"{runner} comes from a build tree rather than an installation",
    )
    minimal = require_minimal_runtime(
        present_modules=[
            name for name in FORBIDDEN_MODULES if importlib.util.find_spec(name)
        ],
        present_tools=[name for name in FORBIDDEN_TOOLS if shutil.which(name)],
    )
    # PythonFMU3 archives carry model source, not a Python runtime. This is
    # the one model dependency the example image has to supply.
    libpython = Path(sysconfig.get_config_var("LIBDIR")) / sysconfig.get_config_var("LDLIBRARY")
    require(libpython.is_file(), f"the model runtime dependency {libpython} is missing")
    images = json.loads((bundle / IMAGES_NAME).read_text())
    executing = os.environ.get("SIL_ACC_EXAMPLE_IMAGE_ID", "")
    require(
        executing == images["example"]["id"],
        f"this image ({executing or 'unidentified'}) is not the bundle's example image "
        f"({images['example']['id']})",
    )
    # The Manifests load the archives from this directory, so they are part of
    # what the bundle index has to cover.
    fmus = {model: file_sha256(FMU_DIR / f"{model}.fmu") for model in MODELS}
    require(
        fmus == index["fmu_sha256"],
        f"the archives at {FMU_DIR} are not the ones the bundle pinned: "
        f"{fmus} against {index['fmu_sha256']}",
    )
    runtime = {
        "sil": _installed_sil(),
        "runner": {"path": str(runner), "sha256": file_sha256(Path(runner))},
        "python": sys.version,
        "machine": [platform.system(), platform.machine(), *platform.libc_ver()],
        "images": images,
        "minimal_runtime": minimal,
        "libpython": {"path": str(libpython), "sha256": file_sha256(libpython)},
        "participant_timeout_ms": PARTICIPANT_TIMEOUT_MS,
        "fmus": fmus,
    }
    write_json(out / "runtime.json", runtime)
    return runtime


def require_pinned_configuration(bundle: Path, name: str, authored: dict) -> None:
    """The consumer material must still author what the bundle was prepared for."""
    pinned = json.loads((bundle / "configurations" / f"{name}.json").read_text())
    require(
        pinned == authored,
        f"{name}: the authored configuration differs from the pinned bundle configuration",
    )


def scenario_check(
    bundle: Path, name: str, config: dict, out: Path, complete_messages: int | None,
) -> dict:
    require_pinned_configuration(bundle, f"scenario-{name}", config)
    reference = _reference(bundle, "scenario", name)["trajectory"]
    result, actual = scenarios.execute(config, out, reference=reference)
    if config["expected_exit"]:
        require(
            complete_messages is not None,
            f"{name}: no completed Run to measure the aborted Recording against",
        )
        result["expected_failure"] = expected_failure_verdict(
            name, result["failure"], complete_messages,
        )
    else:
        require(
            not result["failure"] and result["in_run_coverage"],
            f"{name}: the nominal comparison produced no in-run KPI coverage",
        )
    result["recorded_messages"] = {
        channel: len(rows) for channel, rows in actual.items()
    }
    result["artifacts"] = {
        key: result.pop(key) for key in
        ("manifest_sha256", "authored_manifest_sha256", "recording_sha256")
    }
    return result


def _pinned_observations(bundle: Path, name: str) -> dict:
    """Decode a pinned trajectory on the grid of the row that produced it."""
    reference_row = sensitivity_row(name)
    return reference_observations(_reference(bundle, "sensitivity", name), reference_row)


def sensitivity_check(bundle: Path, name: str, envelope: dict, out: Path) -> dict:
    row = sensitivity_row(name)
    identity = run_manifest_twice(lambda: manifest_for(row), name, out)
    measured = recording_observations(out / identity["recording_files"][0], row)

    own = compare_row(measured, _pinned_observations(bundle, row.name), row)
    require(
        within_reference_tolerance(own),
        f"{name}: this Run differs from its pinned independent trajectory: {own}",
    )
    refined = compare_row(measured, _pinned_observations(bundle, row.reference), row)
    against_baseline = compare_row(
        measured, _pinned_observations(bundle, row.sensitivity_reference), row,
    )
    verdict = envelope_verdict(row, against_baseline, envelope)
    if not row.negative_control:
        envelope_verdict(row, refined, envelope)
    return {
        "row": row.to_document(),
        "artifacts": identity,
        "independent_match": own,
        "comparison": refined,
        "sensitivity_comparison": against_baseline,
        "verdict": verdict,
        "in_run_kpi": single_receipt(out / identity["kpi_log"], KPI_MARKER),
    }


def run(bundle: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    index = acceptance_bundle.verify(bundle)
    runtime = capture_runtime(bundle, index, out)

    configuration = sensitivity_configuration()
    require_pinned_configuration(bundle, "sensitivity", configuration)
    envelope = configuration["acceptance_envelope"]

    # The authored order runs the complete scenario first: its Message count is
    # what makes the aborted Recording's prefix a measurement.
    scenario_results, complete_messages = {}, None
    for name, config in scenario_checks().items():
        scenario_results[name] = scenario_check(
            bundle, name, config, out, complete_messages,
        )
        if not config["expected_exit"]:
            complete_messages = scenario_results[name]["post_hoc_messages"]
    results = {
        "scenarios": scenario_results,
        "sensitivity": {
            name: sensitivity_check(bundle, name, envelope, out)
            for name in SENSITIVITY_CHECKS
        },
    }
    determinism = determinism_summary(results)
    write_json(out / "results.json", results)

    report = {
        "bundle": index,
        "runtime": runtime,
        "results": results,
        "determinism": determinism,
        "fmi_profile": json.loads((bundle / "fmi-profile.json").read_text()),
        "archive_reproducibility": json.loads(
            (bundle / "archive-reproducibility.json").read_text()
        ),
        "handoff": json.loads((bundle / "handoff.json").read_text()),
        "thresholds": {
            "acceptance_envelope": envelope,
            "independent_reference_absolute_si": REFERENCE_ABS_TOL,
            "scenario_kpi": scenario_checks()["nominal"]["kpi"],
        },
        "determinism_policy": (
            "Recordings are compared byte-for-byte only between two executions of one "
            "Manifest. FMU archive reproducibility is judged separately, during "
            "preparation, from two controlled exporter builds."
        ),
    }
    curated = out / "curated"
    curated.mkdir(exist_ok=True)
    write_json(curated / "report.json", report)
    write_json(curated / "raw-sha256.json", {
        path.name: file_sha256(path) for path in sorted(out.iterdir()) if path.is_file()
    })
    return report


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
