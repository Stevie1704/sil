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
    "u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
    "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
    "f32": "float", "f64": "double",
}
