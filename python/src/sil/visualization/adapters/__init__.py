"""Adapters from the headless playback controller to external tools.

Each adapter module imports its tool's SDK at the top, so only the command
that serves through it needs the SDK. Export and the other SiL commands
import no adapter.
"""


class AdapterUnavailable(ImportError):
    """The optional dependency of an adapter is not installed."""
