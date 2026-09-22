"""Regression gate: separate identities, complete/prefix checks, repeatable KPIs."""
import json
import math
import re
import struct
import sys
from pathlib import Path

from sil.recording import read_records
from proof_support import (compare_files, file_sha256, require, run_expecting, run_logged,
                           sil_runner_args as runner_args, write_json)
from scenario_contract import FIELDS, HERE, NAMES, configuration, manifest
from scenario_participant import violations


def records(path):
    result = {name: [] for name in FIELDS}
    for channel, t, raw in read_records(path):
        require(channel in FIELDS, f"unknown Channel {channel}")
        values = list(struct.unpack("<" + "d" * len(FIELDS[channel]), raw))
        result[channel].append((t, values))
    return result


def compare(actual, reference, config, failed=False):
    require(reference["step_ns"] == config["step_ns"] and reference["steps"] == config["steps"],
            "independent grid differs")
    require(reference["initialization"] == [60, 0, 25, 0, 60, 25], "independent initialization differs")
    require(len(reference["intervals"]) == config["steps"], "independent interval count differs")
    for i, row in enumerate(reference["intervals"]):
        require(row["slot_ns"] == i * config["step_ns"], "independent timestamp differs")
    for channel in FIELDS:
        # A failed Run has already published plant/controller values in its
        # abort Slot, but the failing Test participant publishes no freshness.
        end = config["duration_ns"] - config["step_ns"]
        if failed:
            end = actual["truth"][-1][0] - (config["step_ns"] if channel == "freshness" else 0)
        expected = [(row["sensing_publication_ns"] if channel == "sensing" else row["slot_ns"], row[channel])
                    for row in reference["intervals"] if row[channel] is not None and row["slot_ns"] <= end]
        # MCAP iteration sorts by timestamp; ties retain publication order.
        expected.sort(key=lambda item: item[0])
        require(len(actual[channel]) == len(expected), f"{channel}: count {len(actual[channel])} expected {len(expected)}")
        for (t, values), (et, wanted) in zip(actual[channel], expected):
            require(t == et, f"{channel}: timestamp {t} expected {et}")
            require(len(values) == len(wanted) == len(FIELDS[channel]), f"{channel}: invalid width @{t}")
            for field, a, e in zip(FIELDS[channel], values, wanted):
                require(math.isfinite(a) and math.isfinite(e) and math.isclose(a, e,
                        abs_tol=config["absolute_tolerance"], rel_tol=config["relative_tolerance"]),
                        f"{channel}/{field} publication_ns={t} value={a} expected={e} "
                        f"tolerance={config['absolute_tolerance']}+{config['relative_tolerance']}*abs(expected)")


def post_hoc(actual, config):
    errors = []
    for channel, rows in actual.items():
        for t, values in rows:
            errors.extend(violations(channel, dict(zip(FIELDS[channel], values)), t, config))
    return errors


def independent_trajectory(config, config_path, out):
    """Drive the same scenario through FMPy. Needs the comparison tool."""
    name = config["name"]
    reference_path = out / f"{name}.fmpy.json"
    run_logged([sys.executable, HERE / "scenario_reference.py", config_path, reference_path],
               out / f"{name}-reference.log")
    return json.loads(reference_path.read_text())["trajectory"]


def execute(config, out, reference=None):
    """Run one scenario. The acceptance bundle supplies its pinned reference."""
    name = config["name"]
    path = out / f"{name}.json"
    identity = manifest(config).write(path).hash
    repeated = out / f"{name}-authored-again.json"
    manifest(config).write(repeated)
    authored = compare_files(path, repeated)
    config_path = out / f"{name}-configuration.json"
    write_json(config_path, config)
    if reference is None:
        reference = independent_trajectory(config, config_path, out)
    recordings, diagnostics, coverage = [], [], []
    for repeat in (1, 2):
        recording = out / f"{name}-{repeat}.mcap"
        log = run_expecting(runner_args(path, recording), recording.with_suffix(".log"), config["expected_exit"])
        recordings.append(recording)
        actual = records(recording)
        require(actual["truth"], "missing recorded truth prefix")
        compare(actual, reference, config, failed=bool(config["expected_exit"]))
        errors = post_hoc(actual, config)
        if config["expected_exit"]:
            require(errors and errors[0] in log, f"missing matching in-run/post-hoc KPI diagnostic: {log}")
            require(actual["truth"][0][1][0] > config["kpi"]["minimum_gap_m"], "failure already present initially")
            diagnostic = errors[0]
            instant = int(re.search(r"publication_ns=(\d+)", diagnostic)[1])
            require(instant > config["maneuver"]["start_ns"], "failure predates changed behavior")
            diagnostics.append(dict(diagnostic=diagnostic, publication_ns=instant,
                                    abort_slot_ns=actual["truth"][-1][0], prefix_messages=sum(map(len, actual.values()))))
        else:
            require(not errors, str(errors[:3]))
            receipts = [json.loads(line.split("ACC_SCENARIO_KPI ", 1)[1])
                        for line in log.splitlines() if "ACC_SCENARIO_KPI " in line]
            require(receipts == [dict(truth=config["steps"]-1, command=config["steps"]-1)],
                    f"in-run coverage differs: {receipts}")
            coverage = receipts
    hashes = compare_files(*recordings)
    require(not diagnostics or diagnostics[0] == diagnostics[1], "failure instant/diagnostic/prefix differs")
    commands = [v[0] for _, v in actual["command"]]
    low = config["kpi"]["acceleration_min_mps2"]
    high = config["kpi"]["acceleration_max_mps2"]
    require(high in commands, "upper saturation not exercised")
    if name in ("braking", "delayed"):
        require(low in commands, "lower saturation not exercised")
        require(low < commands[-1] < high, "controller did not leave saturation")
    if config["fault"]:
        max_age = max(v[0] for _, v in actual["freshness"])
        require(max_age >= config["minimum_observable_hold_ns"], "sensing hold not observable")
    return dict(manifest_sha256=identity, authored_manifest_sha256=authored,
                recording_sha256=hashes, expected_exit=config["expected_exit"],
                behavior=config["behavior"], failure=diagnostics, in_run_coverage=coverage,
                post_hoc_messages=sum(map(len, actual.values())),
                final_truth_publication_ns=actual["truth"][-1][0],
                minimum_gap_m=min(v[0] for _, v in actual["truth"]),
                acceleration_range=[min(commands), max(commands)]), actual


def run(out):
    from build import build
    from loop_contract import validate_archives
    from qualify import capture_environment, inspect_archives
    out.mkdir(parents=True, exist_ok=True)
    capture_environment(out)
    inspect_archives(out)
    validate_archives(require_sensitivity_inputs=True)
    build(out / "rebuilt")
    archives = {model: compare_files(Path(f"/fmus/{model}.fmu"), out / "rebuilt" / f"{model}.fmu")
                for model in ("AccController", "AccPlant")}
    run_logged([sys.executable, "-m", "pytest", HERE / "test_scenarios.py", "-q"], out / "gate-tests.log")
    results, recordings = {}, {}
    for name in NAMES:
        results[name], recordings[name] = execute(configuration(name), out)
    require(len({r["manifest_sha256"] for r in results.values()}) == len(NAMES), "scenario identities collide")
    for name in ("delayed", "dropped"):
        pairs = zip(recordings["braking"]["command"], recordings[name]["command"])
        differences = [t for ((t, a), (_, b)) in pairs if abs(a[0]-b[0]) > 1e-10]
        require(differences, f"{name} did not change controller behavior")
        results[name]["first_changed_command_ns"] = differences[0]
    write_json(out / "results.json", results)
    curated = out / "curated"
    curated.mkdir(exist_ok=True)
    write_json(curated / "report.json", dict(results=results, archive_rebuild_sha256=archives,
        environment=json.loads((out / "environment.json").read_text()),
        configurations={name: configuration(name) for name in NAMES},
        raw_sha256={p.name: file_sha256(p) for p in sorted(out.iterdir()) if p.is_file()}))


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
