"""OSMP binary variables (OSI Sensor Model Packaging 1.6, FMI 2.0 form).

One binary variable is three fmi2Integer variables: `base.lo` and `base.hi`
hold a 64-bit memory address as two signed 32-bit halves, and `size` holds the
byte count of the serialized OSI message at that address. The address is only
meaningful in the process that loaded the FMU.
"""
from xml.etree import ElementTree

OSMP_TOOL = "net.pmsf.osmp"
OSMP_NS = "{http://xsd.pmsf.net/OSISensorModelPackaging}"
ROLES = ("base.lo", "base.hi", "size")


def _signed(half):
    return half - (1 << 32) if half & 0x80000000 else half


def encode_pointer(address):
    """(lo, hi) as fmi2Integer values, as upstream `encode_pointer_to_integer`."""
    return _signed(address & 0xFFFFFFFF), _signed((address >> 32) & 0xFFFFFFFF)


def decode_pointer(lo, hi):
    return ((hi & 0xFFFFFFFF) << 32) | (lo & 0xFFFFFFFF)


def binary_variables(model_description):
    """Every OSMP binary variable, by name, with the value reference of each role."""
    found = {}
    for variable in ElementTree.fromstring(model_description).iter("ScalarVariable"):
        for tool in variable.iterfind("Annotations/Tool"):
            if tool.get("name") != OSMP_TOOL:
                continue
            annotation = tool.find(f"{OSMP_NS}osmp-binary-variable")
            if annotation is None:
                raise ValueError(f"{variable.get('name')}: OSMP annotation without a binary "
                                 "variable element")
            entry = found.setdefault(annotation.get("name"), {
                "causality": variable.get("causality"),
                "mime_type": annotation.get("mime-type"), "value_references": {}})
            entry["value_references"][annotation.get("role")] = int(variable.get("valueReference"))
    for name, entry in found.items():
        if sorted(entry["value_references"]) != sorted(ROLES):
            raise ValueError(f"OSMP binary variable {name} has roles "
                             f"{sorted(entry['value_references'])}; expected {list(ROLES)}")
    return found
