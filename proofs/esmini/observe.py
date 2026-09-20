"""Wrap one Run and report what it cost, without judging the cost.

Peak wall-clock time and peak resident memory are machine-dependent. They are
recorded here as observations because the proof is asked for them, and they
are deliberately not compared against a threshold: a timing number turned into
a correctness criterion makes a Run fail on a busy machine.

`ru_maxrss` is the largest resident set of any reaped descendant — the kernel
process or one of its participants, whichever peaked highest — not their sum.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import time
from pathlib import Path

KIB = 1024


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: observe.py <observations.json> <command...>")
    destination, command = Path(argv[0]), argv[1:]
    started = time.monotonic()
    completed = subprocess.run(command, check=False)
    elapsed_s = time.monotonic() - started
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    destination.write_text(
        json.dumps(
            {
                "command": command,
                "exit_code": completed.returncode,
                "wall_clock_s": round(elapsed_s, 3),
                "peak_child_rss_bytes": usage.ru_maxrss * KIB,
                "user_cpu_s": round(usage.ru_utime, 3),
                "system_cpu_s": round(usage.ru_stime, 3),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
