"""Export the recorded received CAN frames as the CSV `sil-csv` converts.

Runs in the public-workload tool image (issue #178) with no network, because
decoding `rlog.zst` needs opendbc's log reader and pycapnp, which the SiL
runtime image does not carry. It writes into <out-directory>:

- `frames.csv`: one row per received frame (`src` < 128) of every `can`
  event, in recorded order, with the recorded `logMonoTime` in ns;
- `packet-layout.json`: the binding's `CANPacket_t` bytes compared with
  upstream's own CFFI `make_CANPacket` for every exported frame;
- `frames.json`: the digests and counts that identify the export.

Usage: prepare_frames.py <bundle> <opendbc> <public-workloads> <out-directory>
"""
import csv
import hashlib
import json
import sys
from pathlib import Path

from binding import packet
from workload import FRAME_COLUMNS, TRANSMIT_ECHO_SOURCE

RECORDING = Path("recordings/rlog.zst")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def received_frames(events):
    for log_mono_ns, frames in events:
        for address, src, data in frames:
            if src < TRANSMIT_ECHO_SOURCE:
                yield log_mono_ns, address, src, data


def write_frames(frames, path: Path) -> None:
    with path.open("w", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(FRAME_COLUMNS)
        for log_mono_ns, address, src, data in frames:
            writer.writerow([log_mono_ns, address, src, len(data),
                             *data.ljust(8, b"\x00")])


def packet_layout(frames, make_packet, buffer) -> dict:
    """The binding's packet against upstream's, for every frame."""
    differing = [
        {"address": address, "src": src, "data": data.hex()}
        for _, address, src, data in frames
        if packet(address, src % 4, data) != bytes(buffer(make_packet(address, src % 4, data)))
    ]
    return {"compared": len(frames), "differing": differing[:10],
            "identical": not differing}


def main(bundle, opendbc, public_workloads, out):
    sys.path[:0] = [opendbc, public_workloads]
    from can_recording import read_events
    from opendbc.safety.tests.libsafety import libsafety_py

    bundle, out = Path(bundle), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    contract, events = read_events(bundle / RECORDING)
    frames = list(received_frames(events))
    write_frames(frames, out / "frames.csv")
    layout = packet_layout(frames, libsafety_py.make_CANPacket,
                           libsafety_py.ffi.buffer)
    (out / "packet-layout.json").write_text(json.dumps(layout, indent=2) + "\n")
    identity = {
        "recording": {"path": str(RECORDING), "sha256": sha256(bundle / RECORDING)},
        "frames_csv_sha256": sha256(out / "frames.csv"),
        "exporter_sha256": sha256(Path(__file__)),
        "contract": contract,
        "can_events": len(events),
        "received_frames": len(frames),
        "first_log_mono_ns": events[0][0],
        "last_log_mono_ns": events[-1][0],
    }
    (out / "frames.json").write_text(json.dumps(identity, indent=2) + "\n")
    if not layout["identical"]:
        raise SystemExit(f"the binding's CANPacket_t differs from upstream: "
                         f"{layout['differing']}")


if __name__ == "__main__":
    main(*sys.argv[1:])
