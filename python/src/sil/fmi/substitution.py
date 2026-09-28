"""`sil-fmu-substitute`: replace one FMU of a coupled Run with its Recording.

A Run that `sil-fmu-couple` authored records every Channel. This command
takes that Recording, the coupling document and the FMU archives the Run was
authored from, and the name of one FMU to remove. It writes two files:

* the replacement Manifest. The other FMUs, the retained subsystem, run as
  they did. A Replay participant takes the removed FMU's name and publishes,
  from the Recording, each Channel of the removed FMU that a retained FMU
  takes: the replacement boundary;
* the comparison contract (`sil-compare`) that says whether the retained
  outputs of the replacement Run are those of the original Run.

    sil-fmu-substitute coupling.json --fmu plant plant.fmu \\
        --fmu controller controller.fmu --replace plant \\
        --recording original.mcap -o replacement.json \\
        --contract contract.json --receipt receipt.json

Everything is checked before a file is written:

* the document passes every check of `sil-fmu-couple`, and it and the
  archives author the Manifest whose hash the Recording carries. So the
  Recording is a Run of this experiment, and a retained FMU's start values,
  parameters, Period, priority and routes, and each Latency, are the ones
  the Recording was made with. The Manifest names each archive by its
  absolute path, so the archives must be at the paths the Run named;
* the Recording holds one Message of each compared Channel at each Slot of
  its publisher below the Duration. A Run that failed leaves an incomplete
  Recording;
* the removed FMU feeds at least one retained FMU.

The replacement keeps each retained FMU's participant declaration as it
was: its command, with every start value and binding, its Period, priority
and routes, except a route to the removed FMU. Each boundary Channel keeps
its schema and Latency. The Replay participant is its one publisher, and
the Manifest holds the digest of the Recording it replays. It publishes each
Message at its recorded publication Slot, in the recorded Publish order,
and it has the removed FMU's priority, so it publishes at the removed FMU's
place among the activations of that Slot. The Publish order of each Slot,
and with it the Messages each route holds, is the one of the original Run.
A retained FMU therefore takes each Message at the activation it took it at
in the original Run. A Channel of the removed FMU that no retained FMU takes
is not in the replacement Run.

The contract compares each retained output and each boundary Channel at each
Sample time, from the end of its publisher's first Step through the
Duration: integer fields exactly, float fields with zero tolerance. No
warm-up is left out. The replacement starts from the same initialization, so
the first Sample times, where a retained input still holds its start value,
are compared too, and so is the final one at the Duration. Messages are
compared, not Recordings: the two Runs have different Manifests, and their
Recordings differ in bytes. A byte comparison is the determinism check of
one Manifest, for two Runs of the replacement.

A recorded boundary is the removed FMU's response to the original Run. It
does not respond to anything else. In a closed loop, the replacement is
therefore equivalent to the original Run only for the unchanged retained
experiment. A changed retained model, start value, parameter, Period or
Latency changes what the removed FMU would have computed, and needs a live
Run of it. This command refuses such a change, because the Recording is not
a Run of the changed experiment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mcap.exceptions import McapError

from sil import build_info
from sil._schema_types import INT_RANGES
from sil.fmi.coupling import check, coupled_manifest
from sil.fmi.documents import (
    AuthoringError,
    file_sha256,
    require_distinct,
    sha256,
)
from sil.manifest import ManifestError
from sil.recording import (UnknownRecordingFormat, read_manifest_hash,
                           read_records)

RECEIPT_VERSION = 1
CONTRACT_VERSION = 1
PROG = "sil-fmu-substitute"

VALIDITY = (
    "The Replay participant publishes the removed FMU's recorded response to "
    "the original Run; it does not respond to the retained subsystem. The "
    "retained outputs are expected to equal the original ones only for the "
    "unchanged retained experiment. A changed retained model, start value, "
    "parameter, Period or Latency needs a live Run of the removed FMU."
)

__all__ = ["AuthoringError", "main", "render", "substitute"]


def substitute(document: str | Path, fmus: dict[str, str | Path],
               replaced: str, recording: str | Path, out: str | Path,
               contract: str | Path) -> dict:
    """Write the replacement Manifest to `out` and its comparison contract
    to `contract`; return the receipt. Nothing is written when the
    replacement is refused."""
    document, recording = Path(document), Path(recording)
    out, contract = Path(out), Path(contract)
    fmus = {name: Path(path) for name, path in fmus.items()}
    inputs = {"document": document, "recording": recording,
              **{f"FMU {name!r}": path for name, path in fmus.items()}}
    require_distinct(out, "Manifest", inputs)
    require_distinct(contract, "contract", {**inputs, "Manifest": out})
    coupled = check(document, fmus)
    doc = coupled.doc
    boundary = _boundary(doc, replaced)
    _require_experiment(recording, coupled.manifest.hash())
    compared = sorted(_kept(doc, replaced, boundary))
    publications = _require_publications(recording, doc, compared)
    manifest = _replacement(doc, fmus, replaced, compared, boundary,
                            recording)
    contract_doc = _contract(doc, compared)
    manifest_text = manifest.to_json()
    contract_text = json.dumps(contract_doc, indent=2) + "\n"
    for path, text, what in ((out, manifest_text, "Manifest"),
                             (contract, contract_text, "contract")):
        try:
            path.write_text(text)
        except OSError as e:
            raise AuthoringError(f"cannot write {what} {str(path)!r}: {e}") \
                from e
    return {
        "sil_fmu_substitution_receipt": RECEIPT_VERSION,
        "author": {"name": PROG, "version": build_info.__version__,
                   "revision": build_info.SOURCE_REVISION},
        "document": {"file": document.name,
                     "sha256": sha256(coupled.document_bytes)},
        "fmus": {name: {"file": fmus[name].name,
                        "sha256": file_sha256(fmus[name])}
                 for name in sorted(fmus)},
        "recording": {"file": recording.name,
                      "sha256": file_sha256(recording),
                      "manifest_sha256": coupled.manifest.hash()},
        "manifest": {"file": out.name,
                     "sha256": sha256(manifest_text.encode())},
        "contract": {"file": contract.name,
                     "sha256": sha256(contract_text.encode())},
        "replaced": replaced,
        "retained": sorted(n for n in doc["fmus"] if n != replaced),
        "boundary": [_boundary_entry(doc, channel, coupled.plan,
                                     publications[channel])
                     for channel in boundary],
        "not_replayed": sorted(
            c for c, d in doc["channels"].items()
            if d["publisher"] == replaced and c not in boundary),
        "compared": {
            channel: {"publisher": doc["channels"][channel]["publisher"],
                      **_sample_times(doc, channel)}
            for channel in compared
        },
        "validity": VALIDITY,
    }


# The boundary ------------------------------------------------------------------


def _boundary(doc: dict, replaced: str) -> list[str]:
    """The Channels of the removed FMU that a retained FMU takes."""
    if replaced not in doc["fmus"]:
        raise AuthoringError(
            f"--replace names {replaced!r}, which the document does not "
            f"declare (declared: {', '.join(map(repr, sorted(doc['fmus'])))})"
        )
    boundary = sorted(
        channel for channel, declaration in doc["channels"].items()
        if declaration["publisher"] == replaced
        and set(declaration["subscribers"]) - {replaced}
    )
    if not boundary:
        raise AuthoringError(
            f"FMU {replaced!r} feeds no other FMU, so no Channel of it would "
            f"be replayed; the retained FMUs would run without it"
        )
    return boundary


def _kept(doc: dict, replaced: str, boundary: list[str]) -> list[str]:
    """The Channels of the replacement Run: the boundary and every Channel a
    retained FMU publishes."""
    return [channel for channel, declaration in doc["channels"].items()
            if declaration["publisher"] != replaced or channel in boundary]


def _boundary_entry(doc: dict, channel: str, plan: dict,
                    publications: int) -> dict:
    declaration = doc["channels"][channel]
    return {
        "channel": channel,
        "latency_ns": declaration["latency_ns"],
        "same_slot": declaration["latency_ns"] == 0,
        "fields": [f["name"] for f in declaration["fields"]],
        "publications": publications,
        # Until then each retained input holds its start value.
        "first_delivery_ns": {
            route["subscriber"]: (route["deliveries"][0]["delivered_ns"]
                                  if route["deliveries"] else None)
            for route in plan["routes"] if route["channel"] == channel
        },
    }


# The Recording -----------------------------------------------------------------


def _require_experiment(recording: Path, authored: str) -> None:
    """The Recording is a Run of the Manifest the document authors."""
    try:
        recorded = read_manifest_hash(recording)
    except (OSError, ValueError, McapError, UnknownRecordingFormat) as e:
        raise AuthoringError(
            f"cannot read recording {str(recording)!r}: {e}") from e
    if recorded is None:
        raise AuthoringError(
            f"recording {str(recording)!r} names no Manifest, so no Run wrote "
            f"it; replace an FMU only with the Recording of the coupled Run"
        )
    if recorded != authored:
        raise AuthoringError(
            f"recording {str(recording)!r} is a Run of Manifest {recorded}, "
            f"and this document and these archives author Manifest "
            f"{authored}. The replay is equivalent only for the unchanged "
            f"retained experiment: use the document and the archives, at the "
            f"paths the Run named, that the Recording was made with. A "
            f"changed experiment needs a live Run of the removed FMU"
        )


def _require_publications(recording: Path, doc: dict,
                          compared: list[str]) -> dict[str, int]:
    """Each compared Channel holds one Message at each Slot of its
    publisher; the count of each."""
    times: dict[str, list[int]] = {channel: [] for channel in compared}
    try:
        for channel, t, _ in read_records(recording):
            if channel in times:
                times[channel].append(t)
    except (OSError, ValueError, McapError) as e:
        raise AuthoringError(
            f"cannot read recording {str(recording)!r}: {e}") from e
    for channel in compared:
        expected = _slots(doc, channel)
        if sorted(times[channel]) != expected:
            missing = sorted(set(expected) - set(times[channel]))
            extra = sorted(t for t in times[channel]
                           if t not in expected or times[channel].count(t) > 1)
            gap = (f"has no Message at {missing[0]} ns" if missing
                   else f"has an unexpected Message at {extra[0]} ns")
            raise AuthoringError(
                f"recording {str(recording)!r}: Channel {channel!r} {gap}; "
                f"the Run publishes one at each Slot of "
                f"{doc['channels'][channel]['publisher']!r} below the "
                f"Duration, and a Run that failed leaves an incomplete "
                f"Recording"
            )
    return {channel: len(times[channel]) for channel in compared}


def _period(doc: dict, channel: str) -> int:
    publisher = doc["channels"][channel]["publisher"]
    return doc["fmus"][publisher]["step_period_ns"]


def _slots(doc: dict, channel: str) -> list[int]:
    return list(range(0, doc["duration_ns"], _period(doc, channel)))


# The replacement ---------------------------------------------------------------


def _replacement(doc: dict, fmus: dict[str, Path], replaced: str,
                 kept: list[str], boundary: list[str], recording: Path):
    """The coupled Manifest without the removed FMU and the Channels only it
    needs, and with a Replay participant publishing the boundary."""
    retained = {name: fmu for name, fmu in doc["fmus"].items()
                if name != replaced}
    channels = {
        channel: {**declaration, "subscribers": {
            subscriber: route
            for subscriber, route in declaration["subscribers"].items()
            if subscriber != replaced}}
        for channel, declaration in doc["channels"].items()
        if channel in kept
    }
    manifest = coupled_manifest(
        {**doc, "fmus": retained, "channels": channels},
        {name: fmus[name] for name in retained})
    try:
        manifest.add_replay(replaced, recording=recording.resolve(),
                            channels=boundary,
                            priority=doc["fmus"][replaced]["priority"])
        manifest.to_doc()
    except ManifestError as e:
        raise AuthoringError(str(e)) from None
    return manifest


# The contract ------------------------------------------------------------------


def _sample_times(doc: dict, channel: str) -> dict:
    """The Sample times of one Channel: one Period after each publication,
    from the end of the publisher's first Step through the Duration."""
    period = _period(doc, channel)
    return {"offset_ns": period, "first_ns": period,
            "last_ns": doc["duration_ns"],
            "count": doc["duration_ns"] // period}


def _rule(field_type: str):
    # Both Runs compute on the same machine from the same inputs, so every
    # value is equal; a float compares with zero tolerance.
    return "exact" if field_type in INT_RANGES else {"atol": 0, "rtol": 0}


def _contract(doc: dict, compared: list[str]) -> dict:
    channels = {}
    for channel in compared:
        times = _sample_times(doc, channel)
        channels[channel] = {
            "actual_offset_ns": times["offset_ns"],
            "reference_offset_ns": times["offset_ns"],
            "observations": {"start_ns": times["first_ns"],
                             "stop_ns": times["last_ns"],
                             "step_ns": times["offset_ns"]},
            "fields": {f["name"]: _rule(f["type"])
                       for f in doc["channels"][channel]["fields"]},
        }
    return {"sil_comparison": CONTRACT_VERSION,
            "evaluation": {"from_ns": 0, "to_ns": doc["duration_ns"]},
            "channels": channels}


# The command -------------------------------------------------------------------


def render(receipt: dict) -> str:
    """The receipt as a person reads it."""
    lines = [f"replaced FMU {receipt['replaced']!r} by a Replay participant; "
             f"retained: {', '.join(receipt['retained'])}",
             "boundary Channels, replayed at their recorded publication "
             "Slots:"]
    for entry in receipt["boundary"]:
        first = ", ".join(
            f"{subscriber} first takes one at {ns} ns" if ns is not None
            else f"{subscriber} takes none within the Run"
            for subscriber, ns in entry["first_delivery_ns"].items())
        lines.append(f"  {entry['channel']}: Latency {entry['latency_ns']} "
                     f"ns, {entry['publications']} Messages; {first}")
    if receipt["not_replayed"]:
        lines.append("not in the replacement (no retained FMU takes them): "
                     + ", ".join(receipt["not_replayed"]))
    lines.append("compared at each Sample time, with equal values:")
    lines += [f"  {channel} ({c['publisher']}): {c['count']} Sample times "
              f"from {c['first_ns']} ns through {c['last_ns']} ns"
              for channel, c in receipt["compared"].items()]
    lines.append(receipt["validity"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Replace one FMU of a coupled Run with a replay of its "
                    "recorded Channels, and write the contract that compares "
                    "the retained outputs with the original Run's.",
        allow_abbrev=False,
    )
    parser.add_argument("document", type=Path,
                        help="the coupling document of the recorded Run")
    parser.add_argument("--fmu", nargs=2, action="append", required=True,
                        metavar=("NAME", "PATH"),
                        help="the archive of the FMU the document names NAME")
    parser.add_argument("--replace", required=True, metavar="NAME",
                        help="the FMU to replace by its recorded Channels")
    parser.add_argument("--recording", type=Path, required=True,
                        help="the Recording of the coupled Run")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="the replacement Manifest to write")
    parser.add_argument("--contract", type=Path, required=True,
                        help="the comparison contract to write")
    parser.add_argument("--receipt", type=Path, default=None,
                        help="write the receipt here")
    args = parser.parse_args(argv)
    try:
        fmus: dict[str, Path] = {}
        for name, path in args.fmu:
            if name in fmus:
                raise AuthoringError(f"--fmu {name!r} is given twice")
            fmus[name] = Path(path)
        if args.receipt is not None:
            require_distinct(args.receipt, "receipt", {
                "manifest": args.output, "contract": args.contract,
                "document": args.document, "recording": args.recording,
                **{f"FMU {name!r}": path for name, path in fmus.items()},
            })
        receipt = substitute(args.document, fmus, args.replace,
                             args.recording, args.output, args.contract)
    except AuthoringError as e:
        sys.stderr.write(f"{PROG}: error: {e}\n")
        return 2
    sys.stdout.write(render(receipt))
    if args.receipt is None:
        return 0
    try:
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    except OSError as e:
        sys.stderr.write(f"{PROG}: error: cannot write receipt "
                         f"{str(args.receipt)!r}: {e}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
