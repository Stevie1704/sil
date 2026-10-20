"""Process participant of the profiling qualification (#319).

It publishes, at each Step, digests of what a launch adapter could change:
its arguments, whether its working directory is inside a Run working
directory in the directory where the command started (that directory holds
the qualification's `unrelated.txt`; its path differs per scenario) and its
environment. A profiled
Run must give the same Recording as an ordinary Run, so these digests must
be equal. It also does a known amount of deterministic CPU work in
`probe_spin` and allocates in `probe_alloc`.

    probe_participant.py [--fail-at-step N] [--sleep-at-step N --sleep-ms MS]
                         [--sleep-every-step-ms MS]

--fail-at-step makes the Step fail (a deterministic Run failure).
--sleep-at-step makes one Step slow in wall time (a response deadline).
--sleep-every-step-ms makes every Step slow (a Run that is long enough to
interrupt). Sleeping does not read a clock, so the Recording stays
deterministic.

With SILQUAL_SIDECAR_DIR set, it writes `probe-<pid>.json`: the names of its
environment variables with a SHA-256 of each value, never the values. The
proof uses it to name a difference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from sil.participant import StepParticipant, run

CHANNEL = "probe.out"
SCHEMA = {"fields": [{"name": "step", "type": "u64"},
                     {"name": "argv_digest", "type": "u64"},
                     {"name": "started_in_command_directory", "type": "u64"},
                     {"name": "in_run_directory", "type": "u64"},
                     {"name": "env_digest", "type": "u64"},
                     {"name": "work", "type": "u64"}]}
SPIN_ITERATIONS = 20000
ALLOCATIONS_PER_STEP = 8


def _digest(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


def _environment(cwd: str) -> dict[str, str]:
    # A value that names the Run working directory differs in each Run.
    parts = Path(cwd).parts
    run_directory = next((str(Path(*parts[:i + 1]))
                          for i, part in enumerate(parts)
                          if part.startswith(".sil-run-")), cwd)
    return {key: value.replace(run_directory, "<run-directory>")
            for key, value in sorted(os.environ.items())}


def probe_spin(seed: int) -> int:
    value = seed
    for index in range(SPIN_ITERATIONS):
        value = (value * 6364136223846793005 + index) % (1 << 64)
    return value


def probe_alloc(step: int) -> int:
    blocks = [bytearray(4096 + step % 7) for _ in range(ALLOCATIONS_PER_STEP)]
    return sum(len(block) for block in blocks)


class Probe(StepParticipant):
    def __init__(self, options: argparse.Namespace):
        self.options = options
        cwd = os.getcwd()
        environment = _environment(cwd)
        parts = Path(cwd).parts
        runs = [i for i, part in enumerate(parts)
                if part.startswith(".sil-run-")]
        started = Path(*parts[:runs[0]]) if runs else None
        self.fixed = {
            "argv_digest": _digest(json.dumps(sys.argv)),
            "in_run_directory": int(bool(runs)),
            # The command runs in a directory with the qualification's
            # sentinel file; the Run working directory is inside it.
            "started_in_command_directory": int(
                started is not None and (started / "unrelated.txt").is_file()),
            "env_digest": _digest(json.dumps(environment)),
        }
        sidecar = os.environ.get("SILQUAL_SIDECAR_DIR")
        if sidecar:
            Path(sidecar, f"probe-{os.getpid()}.json").write_text(json.dumps({
                "argv": sys.argv, "in_run_directory":
                self.fixed["in_run_directory"],
                "environment": {key: hashlib.sha256(value.encode())
                                .hexdigest()[:16]
                                for key, value in environment.items()}}))

    def on_step(self, t, dt, inputs):
        step = t // dt
        options = self.options
        if options.fail_at_step is not None and step == options.fail_at_step:
            raise RuntimeError(f"probe fails at Step {step} on purpose")
        if options.sleep_at_step is not None and step == options.sleep_at_step:
            time.sleep(options.sleep_ms / 1000)
        if options.sleep_every_step_ms:
            time.sleep(options.sleep_every_step_ms / 1000)
        work = (probe_spin(step) + probe_alloc(step)) % (1 << 64)
        return [(CHANNEL, {"step": step, "work": work, **self.fixed})]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail-at-step", type=int)
    parser.add_argument("--sleep-at-step", type=int)
    parser.add_argument("--sleep-ms", type=int, default=0)
    parser.add_argument("--sleep-every-step-ms", type=int, default=0)
    run(Probe(parser.parse_args()))


if __name__ == "__main__":
    main()
