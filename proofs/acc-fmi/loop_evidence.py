"""Deterministic, lossless projections of successful closed-loop evidence.

Run automatically by closed_loop.py, or repeat offline:
    python proofs/acc-fmi/loop_evidence.py RAW_DIRECTORY CURATED_DIRECTORY
"""
import csv
import json
import shutil
import sys
from pathlib import Path

from proof_support import write_json


def retain(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for name in ("results.json", "environment.json", "configuration.json", "archives.json",
                 "AccController.identity.json", "AccPlant.identity.json"):
        shutil.copyfile(source / name, target / name)
    # image-id.txt is written by the host script after the container exits.
    if (source / "image-id.txt").exists():
        shutil.copyfile(source / "image-id.txt", target / "image-id.txt")
    for name in ("closed-loop", "shift-command", "shift-sensing", "initial-command", "original-python"):
        for suffix in (".json", "-1.mcap", "-1.mcap.provenance.json"):
            shutil.copyfile(source / (name + suffix), target / (name + suffix))
    initialization = {}
    for mode in ("nominal", "shift-command", "shift-sensing", "initial-command", "original"):
        trace = json.loads((source / (mode + ".fmpy.json")).read_text())
        initialization[mode] = trace["initialization"]
        with (target / (mode + ".fmpy.csv")).open("w", newline="") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(["publication_ns", "interval_end_ns", "gap_m", "relative_speed_mps",
                             "ego_speed_mps", "ego_position_m", "lead_position_m", "lead_speed_mps",
                             "published_command_mps2", "controller_output_mps2", "sampled_gap_m",
                             "sampled_relative_speed_mps", "sampled_ego_speed_mps", "applied_mps2"])
            for row in trace["intervals"]:
                writer.writerow([row["slot_ns"], row["interval_end_ns"], *row["sensing"],
                                 *row["state"], *(row["command"] or [None]), *row["controller_output"],
                                 *row["sampled"], *row["applied"]])
    write_json(target / "initialization.json", initialization)


if __name__ == "__main__":
    retain(Path(sys.argv[1]), Path(sys.argv[2]))
