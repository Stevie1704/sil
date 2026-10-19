"""Markdown summary of one profiling proof results.json (issue #314)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _row(*cells) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def markdown(results: dict) -> str:
    host = results["host"]
    lines = [
        "# Profiling proof results", "",
        _row("Host fact", "Value"), _row("---", "---"),
        _row("kernel", host["uname"]["release"]),
        _row("machine", host["uname"]["machine"]),
        _row("libc", host["libc"]),
        _row("in container", host["in_container"]),
        _row("uid", host["uid"]),
        _row("perf_event_paranoid", host["perf_event_paranoid"]),
        _row("CapEff", host["status"]["CapEff"]),
        _row("Seccomp", host["status"]["Seccomp"]),
        _row("NSpid", host["status"]["NSpid"]),
        *(_row(tool, version) for tool, version in host["tools"].items()),
        "", "## Probes", "",
        _row("Probe", "Outcome", "Exit"), _row("---", "---", "---"),
        *(_row(name, p["outcome"], p.get("returncode"))
          for name, p in results["probes"].items()),
    ]
    if "runs" in results:
        lines += ["", "## Runs (observations, not gates)", "",
                  _row("Run", "Exit", "Wall ms", "Recording bytes",
                       "Collector bytes"),
                  _row("---", "---", "---", "---", "---")]
        for name, run in results["runs"].items():
            if "skipped" in run:
                lines.append(_row(name, "skipped", run["skipped"], "", ""))
                continue
            collector = run.get("perf", {}).get("perf_data_bytes")
            if collector is None and "heaptrack" in run:
                collector = ", ".join(
                    f"{k} {v['bytes']}" for k, v in run["heaptrack"].items())
            lines.append(_row(name, run["returncode"],
                              run["wall_ns"] // 1_000_000,
                              run["recording_bytes"], collector))
        lines += ["", "## Checks and findings", "",
                  _row("Kind", "Result", "Name"), _row("---", "---", "---")]
        for c in results["checks"]:
            state = ("pass" if c["passed"] else "**FAIL**") \
                if c["kind"] == "check" else \
                ("holds" if c["holds"] else "does not hold")
            lines.append(_row(c["kind"], state, c["name"]))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(markdown(json.loads(Path(sys.argv[1]).read_text())), end="")
