"""The fixed-size array FMU of issue #190, as an archive a test can drive.

`tests/fixtures/fmi_array.c` is the binary; this module writes the description
around it. Each input is copied to the output of the same type and shape,
except the [2,3] matrix:

    matrix_out[i][j] = matrix_in[i][j] + bias[i][j] + 10 * (i + 1) + (j + 1)

`expected_matrix` states that rule with explicit indices, independently of
any flattening the importer does, and `row_major` and `column_major` flatten
a matrix the two ways a test compares.

Two Float32 arrays of unequal length and a Float32 scalar share one type
group, and so do a UInt32 array and a UInt32 scalar: an importer that assumes
one value per reference sends an `nValues` the fixture refuses.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

MODEL_IDENTIFIER = "ArrayEcho"
ROWS, COLUMNS = 2, 3
OBJECTS = 8

# name: (value reference, FMI type, causality, shape, unit). The references
# and shapes are the C fixture's.
VARIABLES = {
    "bias": (1, "Float64", "parameter", (ROWS, COLUMNS), None),
    "matrix_in": (2, "Float64", "input", (ROWS, COLUMNS), None),
    "range_in": (3, "Float32", "input", (OBJECTS,), "m"),
    "trim_in": (4, "Float32", "input", (3,), None),
    "gain_in": (5, "Float32", "input", (), None),
    "id_in": (6, "Int32", "input", (OBJECTS,), None),
    "count_in": (7, "UInt32", "input", (), None),
    "class_in": (8, "UInt32", "input", (OBJECTS,), None),
    "sample_in": (9, "UInt64", "input", (OBJECTS,), None),
    "matrix_out": (11, "Float64", "output", (ROWS, COLUMNS), None),
    "range_out": (12, "Float32", "output", (OBJECTS,), "m"),
    "trim_out": (13, "Float32", "output", (3,), None),
    "gain_out": (14, "Float32", "output", (), None),
    "id_out": (15, "Int32", "output", (OBJECTS,), None),
    "count_out": (16, "UInt32", "output", (), None),
    "class_out": (17, "UInt32", "output", (OBJECTS,), None),
    "sample_out": (18, "UInt64", "output", (OBJECTS,), None),
}

# The Schema field type that carries each FMI type.
FIELD_TYPES = {"Float32": "f32", "Float64": "f64", "Int32": "i32",
               "UInt32": "u32", "UInt64": "u64"}


def value_count(name: str) -> int:
    count = 1
    for extent in VARIABLES[name][3]:
        count *= extent
    return count


def field(name: str, field_name: str | None = None) -> dict:
    """The Schema field that carries one variable, under `field_name`."""
    _, kind, _, shape, _ = VARIABLES[name]
    declared = {"name": field_name or name.rsplit("_", 1)[0],
                "type": FIELD_TYPES[kind]}
    if shape:
        declared["count"] = value_count(name)
    return declared


def _element(name: str) -> str:
    reference, kind, causality, shape, unit = VARIABLES[name]
    attributes = {"name": name, "valueReference": str(reference),
                  "causality": causality, "variability": "discrete"}
    if causality == "parameter":
        attributes["variability"] = "fixed"
    if causality != "output":
        attributes["start"] = " ".join(["0"] * value_count(name))
    if unit is not None:
        attributes["unit"] = unit
    text = " ".join(f"{key}={quoteattr(value)}"
                    for key, value in attributes.items())
    dimensions = "".join(f'<Dimension start="{extent}"/>' for extent in shape)
    return f"    <{kind} {text}>{dimensions}</{kind}>"


def description() -> str:
    """`modelDescription.xml` of the fixture, as an exporter would write it."""
    outputs = "\n".join(
        f'    <Output valueReference="{reference}"/>'
        for reference, _, causality, _, _ in VARIABLES.values()
        if causality == "output")
    variables = "\n".join(_element(name) for name in VARIABLES)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<fmiModelDescription fmiVersion="3.0" modelName="{MODEL_IDENTIFIER}"
  instantiationToken="{{9f3c6d0e-1902-4a90-8b19-0000000000a0}}">
  <CoSimulation modelIdentifier="{MODEL_IDENTIFIER}"
    canHandleVariableCommunicationStepSize="true" hasEventMode="false"/>
  <UnitDefinitions><Unit name="m"/></UnitDefinitions>
  <ModelVariables>
    <Float64 name="time" valueReference="0" causality="independent" variability="continuous"/>
{variables}
  </ModelVariables>
  <ModelStructure>
{outputs}
  </ModelStructure>
</fmiModelDescription>
"""


def array_fmu(path: Path, binary: Path, platform_directory: str,
              rewrite=lambda text: text) -> Path:
    """The fixture's archive at `path`, its description passed through
    `rewrite` first."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("modelDescription.xml", rewrite(description()))
        archive.write(binary, f"binaries/{platform_directory}/{binary.name}")
    return path


def expected_matrix(matrix_in: list[list[float]],
                    bias: list[list[float]]) -> list[list[float]]:
    """The fixture's matrix rule, by explicit indices."""
    return [[matrix_in[i][j] + bias[i][j] + 10 * (i + 1) + (j + 1)
             for j in range(COLUMNS)] for i in range(ROWS)]


def row_major(matrix: list[list[float]]) -> list[float]:
    """FMI's order: the last dimension varies fastest."""
    return [matrix[i][j] for i in range(ROWS) for j in range(COLUMNS)]


def column_major(matrix: list[list[float]]) -> list[float]:
    """The transposed order, which no FMI array is flattened in."""
    return [matrix[i][j] for j in range(COLUMNS) for i in range(ROWS)]


def nested(flat: list[float]) -> list[list[float]]:
    """A row-major flattened [2,3] matrix, nested again."""
    return [flat[i * COLUMNS:(i + 1) * COLUMNS] for i in range(ROWS)]
