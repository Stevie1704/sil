"""Independent FMI calls and a kernel Run against the exact same archives."""

import ctypes
import hashlib
import json
import shutil
import struct
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from fmpy import extract, read_model_description
from fmpy.fmi3 import FMU3Slave
from fmpy.fmi1 import FMICallException

ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = ROOT / "build/can"
FRAME = bytes.fromhex("1000000014000000010000000000040001020304")
CONFIRM = bytes.fromhex("200000000c00000001000000")
CONFIG = bytes.fromhex("400000000d00000001a0860100400000000a0000000401")


@contextmanager
def slave(name="SilCanSmoke", logs=None):
    path = ARTIFACTS / f"{name}.fmu"
    description = read_model_description(path, validate=True)
    unpacked = extract(path)
    fmu = FMU3Slave(
        guid=description.guid,
        unzipDirectory=unpacked,
        modelIdentifier=description.coSimulation.modelIdentifier,
        instanceName=name,
    )

    def log(_environment, _status, _category, message):
        if logs is not None:
            logs.append(message.decode())

    fmu.instantiate(eventModeUsed=True, loggingOn=True, logMessage=log)
    try:
        fmu.enterInitializationMode(startTime=0)
        fmu.exitInitializationMode()
        yield fmu
    finally:
        fmu.freeInstance()
        shutil.rmtree(unpacked)


def deliver(fmu, data, node=0):
    fmu.setClock([4 * node + 2], [True])
    fmu.setBinary([4 * node], [data])


def intervals(fmu):
    refs = (ctypes.c_uint32 * 2)(3, 7)
    counters = (ctypes.c_uint64 * 2)()
    resolutions = (ctypes.c_uint64 * 2)()
    qualifiers = (ctypes.c_int * 2)()
    fmu.fmi3GetIntervalFraction(
        fmu.component, refs, 2, counters, resolutions, qualifiers
    )
    return list(counters), list(resolutions), list(qualifiers)


def advance(fmu, start, end):
    fmu.enterStepMode()
    _, terminate, early, last = fmu.doStep(
        currentCommunicationPoint=start, communicationStepSize=end - start
    )
    assert not terminate and not early and last == pytest.approx(end)
    fmu.enterEventMode()


def test_independent_external_exchange():
    logs = []
    with (
        slave("ExternalSender") as sender,
        slave("ExternalReceiver", logs) as receiver,
        slave() as bus,
    ):
        for node, peer in enumerate((sender, receiver)):
            assert peer.getClock([3]) == [True]
            assert peer.getClock([3]) == [False]
            assert peer.getBinary([1]) == [CONFIG]
            deliver(bus, CONFIG, node)
            peer.updateDiscreteStates()
            assert peer.getClock([3]) == [False]
        bus.updateDiscreteStates()
        assert intervals(bus)[2] == [0, 0]
        for fmu in (sender, receiver, bus):
            advance(fmu, 0, 0.3)
        assert sender.getClock([3]) == [True]
        assert sender.getBinary([1]) == [FRAME]
        assert receiver.getClock([3]) == [False]
        deliver(bus, sender.getBinary([1])[0])
        for fmu in (sender, receiver, bus):
            fmu.updateDiscreteStates()
        assert intervals(bus) == ([1, 1], [1000, 1000], [2, 2])
        assert intervals(bus) == ([1, 1], [1000, 1000], [1, 1])
        for fmu in (sender, receiver, bus):
            advance(fmu, 0.3, 0.301)
        bus.setClock([3, 7], [True, True])
        assert bus.getBinary([1, 5]) == [CONFIRM, FRAME]
        deliver(sender, CONFIRM)
        deliver(receiver, FRAME)
        for fmu in (sender, receiver, bus):
            fmu.updateDiscreteStates()
        assert any("Received CAN frame with ID 1 and length 4" in line for line in logs)
        assert intervals(bus)[2] == [0, 0]
        for fmu in (sender, receiver, bus):
            advance(fmu, 0.301, 0.31)
            fmu.updateDiscreteStates()
        assert sender.getClock([3]) == receiver.getClock([3]) == [False]
        report = {
            "event_time_ns": 301000000,
            "sender": CONFIRM.hex(),
            "receiver": FRAME.hex(),
            "receiver_log": logs,
            "fmu_sha256": hashlib.sha256(
                (ARTIFACTS / "SilCanSmoke.fmu").read_bytes()
            ).hexdigest(),
        }
        (ARTIFACTS / "independent.json").write_text(json.dumps(report, indent=2) + "\n")


@pytest.mark.parametrize("size", [0, 1, 8])
def test_sequential_both_directions_and_instances(size):
    payload = bytes(range(size))
    frame = struct.pack("<IIIBBH", 0x10, 16 + size, 0x7FF, 0, 0, size) + payload
    confirm = struct.pack("<III", 0x20, 12, 0x7FF)
    with slave() as bus, slave() as other:
        for node in (0, 1):
            deliver(bus, frame, node)
            bus.updateDiscreteStates()
            assert intervals(bus)[2] == [2, 2]
            assert intervals(other)[2] == [0, 0]
            advance(bus, node * 0.001, (node + 1) * 0.001)
            bus.setClock([3, 7], [True, True])
            assert bus.getBinary([1, 5]) == (
                [confirm, frame] if node == 0 else [frame, confirm]
            )
            bus.updateDiscreteStates()
            assert intervals(bus)[2] == [0, 0]


BAD = [
    b"\x10",  # truncated header
    struct.pack("<II", 0x10, 0),  # invalid length
    FRAME[:-1],  # truncated operation
    FRAME + b"\x00",  # trailing junk
    FRAME[:14] + b"\x05\x00" + FRAME[16:],  # payload mismatch
    struct.pack("<IIIBBH", 0x10, 25, 1, 0, 0, 9) + bytes(9),
    FRAME[:8] + struct.pack("<I", 0x800) + FRAME[12:],
    FRAME[:12] + b"\x01" + FRAME[13:],  # extended
    FRAME[:13] + b"\x01" + FRAME[14:],  # remote
    struct.pack("<II", 0x11, 8),  # CAN FD
    struct.pack("<II", 0x12, 8),  # CAN XL
    struct.pack("<II", 0xDEAD, 8),  # unknown opcode
    struct.pack("<IIBI", 0x40, 13, 1, 500000),  # mismatching bitrate
    struct.pack("<IIBI", 0x40, 13, 2, 100000),  # FD bitrate
    struct.pack("<IIBB", 0x40, 10, 4, 2),  # discard policy
    FRAME + FRAME,  # competing buffer
    CONFIRM,  # wrong direction
]


@pytest.mark.parametrize("payload", BAD)
def test_reject_malformed_and_unsupported(payload):
    with slave() as bus:
        deliver(bus, payload)
        with pytest.raises(FMICallException, match="fmi3UpdateDiscreteStates.*3"):
            bus.updateDiscreteStates()
        # Failure is sticky; a reset restores the initial independent state.
        with pytest.raises(FMICallException):
            bus.enterStepMode()
        bus.reset()
        bus.enterInitializationMode(startTime=0)
        bus.exitInitializationMode()
        assert intervals(bus)[2] == [0, 0]


def test_competing_terminals():
    with slave() as bus:
        deliver(bus, FRAME, 0)
        deliver(bus, FRAME, 1)
        with pytest.raises(FMICallException):
            bus.updateDiscreteStates()


@pytest.mark.parametrize(
    "case",
    [
        "missing_clock",
        "missing_binary",
        "overflow",
        "early_clock",
        "late_step",
        "wrong_reference",
        "wrong_mode",
    ],
)
def test_abi_rejections(case):
    with slave() as bus:
        with pytest.raises(FMICallException):
            if case == "missing_clock":
                bus.setBinary([0], [FRAME])
                bus.updateDiscreteStates()
            elif case == "missing_binary":
                bus.setClock([2], [True])
                bus.updateDiscreteStates()
            elif case == "overflow":
                deliver(bus, bytes(2049))
            elif case == "wrong_reference":
                bus.setClock([99], [True])
            elif case == "wrong_mode":
                bus.doStep(currentCommunicationPoint=0, communicationStepSize=0.001)
            else:
                deliver(bus, FRAME)
                bus.updateDiscreteStates()
                if case == "early_clock":
                    bus.setClock([3, 7], [True, True])
                    bus.getBinary([1])
                else:
                    bus.enterStepMode()
                    bus.doStep(currentCommunicationPoint=0, communicationStepSize=0.002)


def test_sil_group_and_determinism():
    from sil import schema
    from sil.manifest import Manifest
    from sil.recording import read_records

    schemas = {
        "can.Buffer": {
            "fields": [
                {"name": "data_length", "type": "u16"},
                {"name": "data", "type": "u8", "count": 2048},
                {"name": "data_event_time_ns", "type": "u64"},
            ]
        }
    }
    sources = {
        "sender": "sender.CanChannel",
        "receiver": "receiver.CanChannel",
        "confirm": "bus.Node1",
        "frame": "bus.Node2",
    }
    manifest = Manifest(duration_ns=310000000)
    manifest.add_schemas(schemas)
    for channel in sources:
        manifest.add_channel(channel, schema="can.Buffer")
    command = ["python", "-m", "sil.fmi"]
    for name, archive in (
        ("sender", "ExternalSender"),
        ("receiver", "ExternalReceiver"),
        ("bus", "SilCanSmoke"),
    ):
        command += ["--instance", name, str(ARTIFACTS / f"{archive}.fmu")]
    command += [
        "--connect",
        "sender.CanChannel=bus.Node1",
        "--connect",
        "receiver.CanChannel=bus.Node2",
        "--bus-profile",
        "application/org.fmi-standard.fmi-ls-bus.can",
    ]
    for channel, terminal in sources.items():
        command += ["--bind", f"{channel}:data={terminal}.Tx_Data"]
    manifest.add_process(
        "can", command=command, step_period_ns=1000000, publishes=list(sources)
    )
    path = manifest.write(ARTIFACTS / "manifest.json").path
    runner = Path("/opt/kernel/sil-run")
    for name in ("first", "second"):
        result = subprocess.run(
            [
                str(runner),
                str(path),
                "--participant-timeout-ms",
                "5000",
                "-o",
                str(ARTIFACTS / f"{name}.mcap"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        (ARTIFACTS / f"{name}.log").write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
    assert (ARTIFACTS / "first.mcap").read_bytes() == (
        ARTIFACTS / "second.mcap"
    ).read_bytes()
    codec = schema.load(schemas)["can.Buffer"]
    actual = {channel: [] for channel in sources}
    for channel, published, raw in read_records(ARTIFACTS / "first.mcap"):
        fields = codec.unpack(raw)
        actual[channel].append(
            (
                fields["data_event_time_ns"],
                fields["data"][: fields["data_length"]],
                published,
            )
        )
    assert actual == {
        "sender": [(0, CONFIG, 0), (300000000, FRAME, 299000000)],
        "receiver": [(0, CONFIG, 0)],
        "confirm": [(301000000, CONFIRM, 300000000)],
        "frame": [(301000000, FRAME, 300000000)],
    }
    (ARTIFACTS / "sil.json").write_text(
        json.dumps(
            {
                key: [(t, b.hex(), p) for t, b, p in values]
                for key, values in actual.items()
            },
            indent=2,
        )
        + "\n"
    )

    # A real second upstream sender exercises rejection through the Run boundary.
    rejected = json.loads(path.read_text())
    participant = rejected["participants"]["can"]
    participant["command"] = [
        argument.replace("ExternalReceiver.fmu", "ExternalSender.fmu")
        for argument in participant["command"]
    ]
    bad_path = ARTIFACTS / "competing.json"
    bad_path.write_text(json.dumps(rejected) + "\n")
    result = subprocess.run(
        [
            str(runner),
            str(bad_path),
            "--participant-timeout-ms",
            "5000",
            "--no-recording",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    (ARTIFACTS / "competing.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "fmi3UpdateDiscreteStates" in result.stderr


def test_package_identity_metadata_and_linkage():
    import zipfile
    from lxml import etree
    import fmpy

    for name in ("SilCanSmoke", "ExternalSender", "ExternalReceiver"):
        path = ARTIFACTS / f"{name}.fmu"
        with zipfile.ZipFile(path) as archive:
            identity = json.loads(archive.read("resources/identity.json"))
            for source, expected in identity["sources"].items():
                assert (
                    hashlib.sha256(archive.read(f"sources/{source}")).hexdigest()
                    == expected
                )
            manifest = etree.fromstring(
                archive.read("extra/org.fmi-standard.fmi-ls-bus/fmi-ls-manifest.xml")
            )

            class OfflineSchema(etree.Resolver):
                def resolve(self, url, _public_id, context):
                    if url.endswith("/fmi3LayeredStandardManifest.xsd"):
                        bundled = (
                            Path(fmpy.__file__).parent
                            / "schema/fmi3/fmi3LayeredStandardManifest.xsd"
                        )
                        return self.resolve_filename(str(bundled), context)
                    return None

            parser = etree.XMLParser(no_network=True)
            parser.resolvers.add(OfflineSchema())
            validator = etree.XMLSchema(
                etree.parse(
                    "/opt/spec/schema/fmi3LayeredStandardBusManifest.xsd", parser
                )
            )
            validator.assertValid(manifest)
            assert (
                manifest.get("{http://fmi-standard.org/fmi-ls-manifest}fmi-ls-version")
                == "1.0.0"
            )
            assert manifest.get("isBusSimulationFMU", "false") == (
                "true" if name == "SilCanSmoke" else "false"
            )
            assert (
                "documentation/licenses/LICENSE" in archive.namelist()
                or "documentation/licenses/LICENSE.txt" in archive.namelist()
            )
        read_model_description(path, validate=True)
    with slave() as bus:
        library = Path(bus.unzipDirectory) / "binaries/x86_64-linux/SilCanSmoke.so"
        symbols = subprocess.check_output(["nm", "-D", str(library)], text=True)
        assert "fmi3InstantiateCoSimulation" in symbols
        for forbidden in ("clock_gettime", "gettimeofday", " rand", "sil_"):
            assert forbidden not in symbols


def test_unsupported_capability_is_explicit():
    with slave() as bus:
        with pytest.raises(FMICallException):
            bus.getFMUState()
