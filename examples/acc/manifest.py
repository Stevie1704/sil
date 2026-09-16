"""Compatibility entry point for the packaged ACC reference Run.

The implementation lives in ``sil.examples.acc`` so the same Manifest can be
built from a source checkout or an installed wheel.
"""

from pathlib import Path

from sil.examples.acc.manifest import (
    ACC_SCHEMAS,
    DURATION_NS,
    ROUTE_CAPACITY,
    SENSING_DELAY_END_NS,
    SENSING_DELAY_NS,
    SENSING_DELAY_START_NS,
    SENSING_ROUTE_CAPACITY,
    STEP_PERIOD_NS,
    acc_manifest,
    main,
)
from sil.manifest import Manifest, SubscriberRoute
from sil.testing import participant_command

EXAMPLE_DIR = Path(__file__).resolve().parent
ROOT = EXAMPLE_DIR.parents[1]


if __name__ == "__main__":
    raise SystemExit(main())
