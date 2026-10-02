"""Drive the OSMP FMUs under FMPy, an independent FMI 2.0 importer.

Both FMUs link OSI statically and the system `libprotobuf` dynamically. Each
one therefore registers the OSI .proto files in the one process-wide Protobuf
pool, and the second registration aborts the process ("File already exists in
database"). One process can thus hold only one of these shared objects, loaded
from one path. `prepare.py` runs the sensor in its own process, and this module
runs the source and the co-load check as child processes:

    drive.py source <OSMPDummySource.fmu> <unzip-directory>
    drive.py co-load <OSMPDummySource.fmu> <OSMPDummySensor.fmu> <work-directory>

Each prints one JSON object. The OSI Python classes must be on PYTHONPATH.
"""
import ctypes
import json
import sys
import zipfile
from pathlib import Path

import osmp
import scene


class Fmu:
    """One FMI 2.0 Co-Simulation instance, left in initialization mode until `start`.

    `unzip` must be the same directory for every instance of one archive, so
    that the shared object is loaded once."""

    def __init__(self, archive, unzip, name, reals=None):
        from fmpy import extract, read_model_description
        from fmpy.fmi2 import FMU2Slave
        description = read_model_description(str(archive))
        with zipfile.ZipFile(archive) as opened:
            self.binary = osmp.binary_variables(opened.read("modelDescription.xml").decode())
        if not Path(unzip).exists():
            extract(str(archive), unzipdir=str(unzip))
        self.refs = {v.name: v.valueReference for v in description.modelVariables}
        self.fmu = FMU2Slave(guid=description.guid, unzipDirectory=str(unzip),
                             modelIdentifier=description.coSimulation.modelIdentifier,
                             instanceName=name)
        self.fmu.instantiate()
        for variable, value in (reals or {}).items():
            self.fmu.setReal([self.refs[variable]], [value])
        self.fmu.setupExperiment(startTime=0.0)
        self.fmu.enterInitializationMode()

    def start(self):
        self.fmu.exitInitializationMode()

    def integers(self, binary):
        refs = self.binary[binary]["value_references"]
        return self.fmu.getInteger([refs[role] for role in osmp.ROLES])

    def set_integers(self, binary, values):
        refs = self.binary[binary]["value_references"]
        self.fmu.setInteger([refs[role] for role in osmp.ROLES], list(values))

    def message(self, binary, message):
        """The OSI message behind one binary variable, and its size in bytes."""
        lo, hi, size = self.integers(binary)
        if size <= 0:
            raise RuntimeError(f"{binary} is empty")
        message.ParseFromString(ctypes.string_at(osmp.decode_pointer(lo, hi), size))
        return message, size

    def close(self):
        self.fmu.terminate()
        self.fmu.freeInstance()


class Buffer:
    """Importer-owned bytes for one OSMP input; keep it alive while the FMU reads it."""

    def __init__(self, data):
        self.size = len(data)
        self.memory = ctypes.create_string_buffer(data, self.size)

    def integers(self):
        return (*osmp.encode_pointer(ctypes.addressof(self.memory)), self.size)


def pose(base):
    return {"x": base.position.x, "y": base.position.y, "z": base.position.z,
            "yaw": base.orientation.yaw, "pitch": base.orientation.pitch,
            "roll": base.orientation.roll}


def sensor_view(time):
    """The closed-form ground truth at `time` as a serialized osi3::SensorView,
    with the identifiers and timestamps OSMPDummySource sets."""
    from osi_sensorview_pb2 import SensorView
    view = SensorView()
    seconds, nanos = scene.timestamp(time)
    view.sensor_id.value = scene.SENSOR_ID
    view.host_vehicle_id.value = scene.HOST_ID
    view.timestamp.seconds, view.timestamp.nanos = seconds, nanos
    truth = view.global_ground_truth
    truth.host_vehicle_id.value = scene.HOST_ID
    truth.timestamp.seconds, truth.timestamp.nanos = seconds, nanos
    for vehicle in scene.ground_truth(time):
        added = truth.moving_object.add()
        added.id.value = vehicle["id"]
        base = added.base
        base.position.x, base.position.y, base.position.z = (vehicle[a] for a in "xyz")
        base.orientation.yaw = vehicle["yaw"]
        base.orientation.pitch = vehicle["pitch"]
        base.orientation.roll = vehicle["roll"]
        base.dimension.length = vehicle["length"]
        base.dimension.width = vehicle["width"]
        base.dimension.height = vehicle["height"]
    return view.SerializeToString()


def view_divergence(view, time):
    """The first difference between one SensorView and the closed-form source."""
    want = {"host": scene.HOST_ID, "sensor": scene.SENSOR_ID, "mounting": False,
            "timestamp": scene.timestamp(time), "objects": scene.ground_truth(time)}
    truth = view.global_ground_truth
    got = {"host": truth.host_vehicle_id.value, "sensor": view.sensor_id.value,
           "mounting": view.HasField("mounting_position"),
           "timestamp": (view.timestamp.seconds, view.timestamp.nanos),
           "objects": [{"id": o.id.value, **pose(o.base),
                        "length": o.base.dimension.length, "width": o.base.dimension.width,
                        "height": o.base.dimension.height} for o in truth.moving_object]}
    for field in ("host", "sensor", "mounting", "timestamp"):
        if want[field] != got[field]:
            return {"field": field, "expected": want[field], "actual": got[field]}
    if [o["id"] for o in want["objects"]] != [o["id"] for o in got["objects"]]:
        return {"field": "ids", "actual": [o["id"] for o in got["objects"]]}
    for expected, actual in zip(want["objects"], got["objects"]):
        for field, value in expected.items():
            if abs(actual[field] - value) > scene.TOL:
                return {"object": expected["id"], "field": field, "expected": value,
                        "actual": actual[field]}
    return None


def check_source(archive, unzip, steps):
    """Every SensorView the source produces must equal the closed form."""
    from osi_sensorview_pb2 import SensorView
    source = Fmu(archive, unzip, "source")
    source.start()
    sizes = []
    for step in range(steps):
        point = step * scene.STEP_S
        source.fmu.doStep(point, scene.STEP_S)
        view, size = source.message("OSMPSensorViewOut", SensorView())
        divergence = view_divergence(view, point + scene.STEP_S)
        if divergence is not None:
            raise RuntimeError(f"SensorView at step {step} leaves the closed form: {divergence}")
        sizes.append(size)
    source.close()
    return {"steps": steps, "agrees_with_closed_form": True,
            "sensor_view_bytes": {"min": min(sizes), "max": max(sizes)}}


def co_load(source_archive, sensor_archive, work):
    """Load both FMUs into this one process, as one OSMP connection would need."""
    for name, archive in (("source", source_archive), ("sensor", sensor_archive)):
        Fmu(archive, Path(work) / name, name).start()
    return {"loaded": True}


if __name__ == "__main__":
    mode, *arguments = sys.argv[1:]
    if mode == "source":
        result = check_source(arguments[0], arguments[1], int(arguments[2]))
    else:
        result = co_load(*arguments)
    print(json.dumps(result))
