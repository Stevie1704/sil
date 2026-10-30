"""Tool-independent interfaces to external visualization tools (spec #345).

`export_recording` writes a finalized Recording as an indexed MCAP in the
`sil_visualization_json` profile, which file players such as Foxglove open
without a SiL server. `interchange` holds that profile's schema and payload
conversion for reuse by later adapters. `prepare_navigation` indexes a
Recording for bounded chronological reads and exact snapshots.
`open_playback` returns the headless `Playback` controller that tool
adapters drive.
"""

from sil.visualization.export import (ExportInputError, ExportOutputError,
                                      export_recording)
from sil.visualization.navigation import prepare_navigation
from sil.visualization.playback import Playback, open_playback

__all__ = ["ExportInputError", "ExportOutputError", "Playback",
           "export_recording", "open_playback", "prepare_navigation"]
