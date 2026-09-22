"""Primitive wire facts, also installed beside the dependency-free silschema tool.

Widths come from explicitly little-endian struct formats (never native ABI
sizes); integer bounds follow from those widths. C spellings are the only
additional language mapping. The kernel deliberately validates independently;
tests/fixtures/schema_conformance.json pins actual bytes across both languages.
"""

import struct

FORMATS = {
    "u8": "B", "u16": "H", "u32": "I", "u64": "Q",
    "i8": "b", "i16": "h", "i32": "i", "i64": "q",
    "f32": "f", "f64": "d",
}
SIZES = {name: struct.calcsize("<" + fmt) for name, fmt in FORMATS.items()}
INT_RANGES = {
    name: (0, (1 << (size * 8)) - 1) if name.startswith("u") else
          (-(1 << (size * 8 - 1)), (1 << (size * 8 - 1)) - 1)
    for name, size in SIZES.items() if name[0] in "ui"
}
C_TYPES = {
    name: ("uint" if name[0] == "u" else "int") + str(size * 8) + "_t"
    for name, size in SIZES.items() if name[0] in "ui"
} | {"f32": "float", "f64": "double"}
