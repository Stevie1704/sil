"""Tool-independent interfaces to external visualization tools (spec #345).

`export_recording` writes a finalized Recording as an indexed MCAP in the
`sil_visualization_json` profile, which file players such as Foxglove open
without a SiL server. `interchange` holds that profile's schema and payload
conversion for reuse by later adapters. `prepare_navigation` indexes a
Recording for bounded chronological reads and exact snapshots.
"""

from sil.visualization.export import (ExportInputError, ExportOutputError,
                                      export_recording)
from sil.visualization.navigation import prepare_navigation

__all__ = ["ExportInputError", "ExportOutputError", "export_recording",
           "prepare_navigation"]
