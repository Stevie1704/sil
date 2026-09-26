"""The comparison contract for a replay window against the full history.

`sil-window` selects a window of `history.csv`'s Recording and rebases it to
Virtual time zero. The library is then run twice: once over the full history
from its first Message, once over the window from its warm-up. This script
writes the `sil-compare` contract that compares the two Runs over the
evaluation interval only, taken from the `sil-window` receipt:

- The windowed Run is the actual side, in its own Virtual time (offset 0).
  The full-history Run is the reference; its Virtual time is the source time,
  so its offset is minus the window's source origin.
- The evaluation window is the receipt's `evaluation_window`. The warm-up is
  outside it and is not compared.
- Each output is observed at every Step from the evaluation start to the last
  Step before the windowed Run's Duration.
- `filtered_speed_mps` must agree within `ATOL`. `cycles` is ignored: it
  counts cycles since the library's init, which is later in the window.

    python examples/library/window_contract.py window.receipt.json \\
        -o contract.json
"""

import argparse
import json
from pathlib import Path

from manifest import INSTANCES, STEP_PERIOD_NS

# The filter forgets its initial state by a factor (1 - gain) per Step: 2/3
# for filter.fast and 5/6 for filter.slow. After the example's 100 warm-up
# Steps a state difference of a few m/s is below 1e-7 m/s.
ATOL = 1e-6


def window_contract(receipt: dict) -> dict:
    """The contract over the evaluation interval the `receipt` states."""
    evaluation = receipt["evaluation_window"]
    return {
        "sil_comparison": 1,
        "evaluation": dict(evaluation),
        "channels": {
            channel: {
                "actual_offset_ns": 0,
                "reference_offset_ns": -receipt["source_origin_ns"],
                "observations": {
                    "start_ns": evaluation["from_ns"],
                    "stop_ns": receipt["duration_ns"] - STEP_PERIOD_NS,
                    "step_ns": STEP_PERIOD_NS,
                },
                "fields": {"filtered_speed_mps": {"atol": ATOL, "rtol": 0},
                           "cycles": "ignore"},
            }
            for channel in INSTANCES
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("receipt", type=Path, help="the sil-window receipt")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="path to write the contract to")
    args = parser.parse_args()
    receipt = json.loads(args.receipt.read_text())
    args.output.write_text(json.dumps(window_contract(receipt), indent=2)
                           + "\n")
