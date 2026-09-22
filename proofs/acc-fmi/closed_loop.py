"""Execute the closed-loop proof and retain its positive and negative evidence."""
import json
import sys
from functools import partial
from pathlib import Path

from sil.examples.acc.manifest import acc_manifest
from loop_compare import compare, first_difference, recording_messages, validate_reference
from loop_contract import (HERE, MIN_GAP_M, PYTHON_SCHEDULE, STEP_NS, STEPS, VARIANTS,
                           configuration, manifest, validate_archives, validate_manifest)
from loop_evidence import retain
from proof_support import (compare_files, require, run_expecting, run_logged,
                           sil_runner_args as runner_args, write_json)


def execute(factory, name, out):
    # Reconstruct independently: serializing the same object twice would miss
    # mutable defaults or accidental state in a Manifest authoring function.
    path = out / f"{name}.json"
    identity = factory().write(path).hash
    repeat_path = out / f"{name}-authored-again.json"
    factory().write(repeat_path)
    authored_hashes = compare_files(path, repeat_path)
    validate_manifest(json.loads(path.read_text()))
    recordings = [out / f"{name}-{n}.mcap" for n in (1, 2)]
    for recording in recordings:
        run_logged(runner_args(path, recording), recording.with_suffix(".log"))
    return recordings[0], dict(manifest_sha256=identity, authored_manifest_sha256=authored_hashes,
                              recording_sha256=compare_files(*recordings))


def reject_invalid_bindings(out):
    diagnostics = {}
    for label, variable, diagnostic in (
        ("unknown", "unknown", "does not declare"),
        ("causality", "accel_mps2", "causality"),
    ):
        path = out / f"invalid-{label}.json"
        document = manifest().to_doc()
        command = document["participants"]["controller"]["command"]
        command[command.index("sensing:gap_m=gap_m")] = f"sensing:gap_m={variable}"
        write_json(path, document)
        output = run_expecting(runner_args(path, path.with_suffix(".mcap")), path.with_suffix(".log"), 2)
        require(diagnostic in output, f"invalid {label} binding failed for another reason: {output}")
        diagnostics[label] = dict(exit_code=2, diagnostic=diagnostic)
    return diagnostics


def reject_runtime_faults(out):
    diagnostics = {}
    for name, factory, diagnostic in (
        ("overflow", partial(manifest, "shift-sensing", capacity=2), "capacity 2"),
        ("kpi", partial(manifest, minimum_gap_m=61.0), "minimum-gap KPI"),
    ):
        path = out / f"invalid-{name}.json"
        factory().write(path)
        output = run_expecting(runner_args(path, path.with_suffix(".mcap")), path.with_suffix(".log"), 1)
        require(diagnostic in output, f"{name} failed for another reason: {output}")
        diagnostics[name] = dict(exit_code=1, diagnostic=diagnostic)
    return diagnostics


def check_kpi_coverage(recording):
    log = recording.with_suffix(".log").read_text()
    receipts = [json.loads(line.split("ACC_KPI ", 1)[1]) for line in log.splitlines() if "ACC_KPI " in line]
    expected = dict(checked_messages=STEPS - 1, last_publication_ns=(STEPS - 2) * STEP_NS)
    require(receipts == [expected], f"in-run KPI coverage differs: {receipts}")
    return expected


def run(out):
    # Archive/environment audits need FMPy only inside the execution image.
    # Keep ordinary comparator, Manifest and evidence tests FMI-independent.
    from qualify import capture_environment, inspect_archives

    out.mkdir(parents=True, exist_ok=True)
    capture_environment(out)
    inspect_archives(out)
    validate_archives()
    run_logged([sys.executable, "-m", "pytest", HERE / "test_closed_loop.py", "-q"],
               out / "gate-tests.log")
    config_path = out / "configuration.json"
    write_json(config_path, configuration())
    references = {}
    for mode in (*VARIANTS, "original"):
        path = out / f"{mode}.fmpy.json"
        run_logged([sys.executable, HERE / "loop_reference.py", mode, path, config_path], path.with_suffix(".log"))
        references[mode] = json.loads(path.read_text())
        validate_reference(references[mode])
    recordings, results, messages = {}, {}, {}
    for mode in VARIANTS:
        name = "closed-loop" if mode == "nominal" else mode
        recordings[mode], results[mode] = execute(partial(manifest, mode), name, out)
        messages[mode] = recording_messages(recordings[mode])
        compare(messages[mode], references[mode])
    nominal = messages["nominal"]
    minimum = min(row["sensing"][0] for row in nominal.values())
    require(minimum >= MIN_GAP_M, f"minimum gap {minimum} < {MIN_GAP_M}")
    # The published final state has no in-run delivery. Check every Recording
    # Message post-hoc and retain the independently counted in-run coverage.
    coverage = check_kpi_coverage(recordings["nominal"])
    rejected = {}
    for mode in VARIANTS[1:]:
        try:
            compare(messages[mode], references["nominal"])
        except RuntimeError as error:
            rejected[mode] = str(error)
        else:
            raise RuntimeError(f"negative control {mode} was not detected")
    necessity = dict(
        sensing_changes_command=first_difference(nominal, messages["shift-sensing"], "command"),
        command_changes_motion=first_difference(nominal, messages["shift-command"], "state"))
    original, original_results = execute(acc_manifest, "original-python", out)
    compare(recording_messages(original, PYTHON_SCHEDULE), references["original"], PYTHON_SCHEDULE)
    write_json(out / "results.json", dict(
        runs=results, original_python=original_results, minimum_gap_m=minimum,
        checked_communication_points=STEPS, checked_messages=sum(map(len, nominal.values())),
        in_run_kpi=coverage, post_hoc_final_publication_ns=max(nominal),
        negative_controls=rejected, sil_behavioral_necessity=necessity,
        invalid_bindings=reject_invalid_bindings(out), runtime_faults=reject_runtime_faults(out)))
    retain(out, out / "curated")


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
