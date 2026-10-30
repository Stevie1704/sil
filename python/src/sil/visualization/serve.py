"""`sil playback serve`: play a Recording to an external tool (spec #345 s3).

The command validates and indexes the source, starts the headless playback
controller paused, starts the adapter's server and prints its address. The
user opens that address in the external tool, whose playback controls then
drive the controller. It serves until SIGINT or SIGTERM.

Exit codes: 0 stopped by a signal, 2 rejected input or configuration
(including a missing adapter extra), 1 an I/O failure such as a port in use.
"""

from __future__ import annotations

import argparse
import importlib
import signal
import sys
import threading
from pathlib import Path

from sil.visualization.adapters import AdapterUnavailable
from sil.visualization.navigation import IndexStorageError
from sil.visualization.playback import default_cache_dir, open_playback
from sil.visualization.source import SourceError

PROG = "sil playback serve"
EXIT_INPUT = 2
EXIT_IO = 1
ADAPTERS = {"foxglove": ("sil.visualization.adapters.foxglove", "foxglove")}
DEFAULT_LISTEN = "127.0.0.1:8765"


def parse_listen(text: str) -> tuple[str, int]:
    host, separator, port = text.rpartition(":")
    if not separator or not host or not port.isdigit() or int(port) > 65535:
        raise ValueError(f"--listen must be HOST:PORT, not {text!r}")
    return host.strip("[]"), int(port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Serve a finalized Recording to an external tool, whose "
                    "own playback controls (play, pause, seek, speed) drive "
                    "SiL's headless playback. Stop it with Ctrl-C.",
        allow_abbrev=False,
    )
    parser.add_argument("source", type=Path, help="the finalized .mcap Recording")
    parser.add_argument("--adapter", choices=sorted(ADAPTERS), default="foxglove",
                        help="the external tool (default: foxglove)")
    parser.add_argument("--listen", default=DEFAULT_LISTEN, metavar="HOST:PORT",
                        help=f"the address to serve on (default: {DEFAULT_LISTEN}; "
                             "loopback only, one client at a time)")
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="where the disposable navigation index is kept "
                             f"(default: {default_cache_dir()})")
    args = parser.parse_args(argv)
    try:
        host, port = parse_listen(args.listen)
    except ValueError as e:
        return _error(e, EXIT_INPUT)
    module, extra = ADAPTERS[args.adapter]
    try:
        adapter = importlib.import_module(module)
    except AdapterUnavailable as e:
        return _error(f"{e}; install it with: pip install 'sil[{extra}]'",
                      EXIT_INPUT)
    try:
        playback = open_playback(args.source, cache_dir=args.cache_dir)
    except IndexStorageError as e:
        return _error(e, EXIT_IO)
    except SourceError as e:
        return _error(e, EXIT_INPUT)
    except OSError as e:
        return _error(e, EXIT_IO)
    try:
        bridge = adapter.serve(playback, host, port)
    except adapter.UnsupportedSource as e:
        playback.close()
        return _error(e, EXIT_INPUT)
    except OSError as e:
        playback.close()
        return _error(f"cannot listen on {args.listen}: {e}", EXIT_IO)
    stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stop.set())
    sys.stdout.write(f"serving {args.source} to {args.adapter} at {bridge.url}\n")
    sys.stdout.flush()
    try:
        stop.wait()
    finally:
        bridge.close()
    return 0


def _error(message, code: int) -> int:
    sys.stderr.write(f"{PROG}: error: {message}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
