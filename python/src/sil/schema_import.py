"""Import flat SiL Schemas from the DWARF layout of compiled C types.

`sil-schema-import` reads the DWARF type information of an ELF object and
writes, for each requested C type, a flat Schema whose packed layout is
byte-identical to the type's compiled layout, and a C layout-check header
that fails the build when the Schema and the real layout differ. An adapter
can then copy between the project struct and the SiL struct with memcpy.

Flattening: nested members join their path with ``_``; an array of
primitives becomes one field with ``count`` equal to the product of its
dimensions; an array of structs unrolls per element (``objects_3_x``);
padding becomes ``u8`` fields named ``_sil_pad_<offset>``. Unions,
bit-fields, pointers, flexible array members and primitives without a Schema
type are rejected with the type and the member path.

Needs pyelftools, installed through the ``sil[dwarf]`` extra.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from math import prod
from pathlib import Path
from typing import NoReturn

from ._schema_types import ident_problem, schema_name_problem

PAD_PREFIX = "_sil_pad_"

# DW_ATE_* base-type encodings.
_BOOLEAN = 0x02
_FLOAT = 0x04
_SIGNED = {0x05, 0x06}  # signed, signed_char
_UNSIGNED = {0x07, 0x08, 0x10}  # unsigned, unsigned_char, UTF
_INT_SIZES = {1, 2, 4, 8}
_FLOAT_TYPES = {4: "f32", 8: "f64"}
_QUALIFIERS = {"DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
               "DW_TAG_restrict_type"}
_PRIMITIVE_TAGS = ("DW_TAG_base_type", "DW_TAG_enumeration_type")
_REJECTED_TAGS = {
    "DW_TAG_union_type": "union",
    "DW_TAG_pointer_type": "pointer",
    "DW_TAG_reference_type": "reference",
    "DW_TAG_rvalue_reference_type": "reference",
    "DW_TAG_ptr_to_member_type": "pointer to member",
    "DW_TAG_class_type": "C++ class",
    "DW_TAG_atomic_type": "_Atomic type",
    "DW_TAG_subroutine_type": "function type",
}


class ImportRejected(Exception):
    """The import cannot mirror a layout; each problem names type and path."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Field:
    """One Schema field at a byte offset of the original type.

    `member` is the C member designator for the layout check (``ego.pose.x``,
    ``objects[3].x``); padding has none.
    """

    name: str
    type: str
    count: int | None
    offset: int
    size: int
    member: str | None

    def to_schema(self) -> dict:
        entry = {"name": self.name, "type": self.type}
        if self.count is not None:
            entry["count"] = self.count
        return entry


@dataclass(frozen=True)
class Layout:
    c_type: str  # the spelling of the original type in C
    schema_name: str
    size: int
    fields: list[Field]


class _Rejection(Exception):
    def __init__(self, member: str, construct: str):
        super().__init__(construct)
        self.member = member
        self.construct = construct


def _name(die) -> str | None:
    attr = die.attributes.get("DW_AT_name")
    return attr.value.decode() if attr else None


def _type_of(die):
    return die.get_DIE_from_attribute("DW_AT_type")


def _unqualified(die):
    """The type under typedefs and const/volatile/restrict qualifiers."""
    while die.tag in _QUALIFIERS:
        die = _type_of(die)
    return die


def _byte_size(die) -> int:
    return die.attributes["DW_AT_byte_size"].value


def _primitive(die, member: str) -> str:
    """The Schema type of a base or enumeration type."""
    size = _byte_size(die)
    if die.tag == "DW_TAG_enumeration_type":
        return ("i" if _enum_is_signed(die) else "u") + str(size * 8)
    encoding = die.attributes["DW_AT_encoding"].value
    if encoding == _BOOLEAN and size == 1:
        return "u8"
    if encoding in _SIGNED | _UNSIGNED and size in _INT_SIZES:
        return ("i" if encoding in _SIGNED else "u") + str(size * 8)
    if encoding == _FLOAT and size in _FLOAT_TYPES:
        return _FLOAT_TYPES[size]
    raise _Rejection(member, _name(die) or die.tag)


def _enum_is_signed(die) -> bool:
    if "DW_AT_type" in die.attributes:
        underlying = _unqualified(_type_of(die))
        return underlying.attributes["DW_AT_encoding"].value in _SIGNED
    return any(e.attributes["DW_AT_const_value"].value < 0
               for e in die.iter_children() if e.tag == "DW_TAG_enumerator")


def _dimensions(die, member: str) -> list[int]:
    dims = []
    for sub in die.iter_children():
        if sub.tag != "DW_TAG_subrange_type":
            continue
        if "DW_AT_count" in sub.attributes:
            count = sub.attributes["DW_AT_count"].value
        elif "DW_AT_upper_bound" in sub.attributes:
            count = sub.attributes["DW_AT_upper_bound"].value + 1
        else:
            count = 0
        if not isinstance(count, int) or count <= 0:
            raise _Rejection(member, "flexible array member")
        dims.append(count)
    return dims


def _member_offset(die, member: str) -> int:
    if "DW_AT_bit_size" in die.attributes:
        raise _Rejection(member, "bit-field")
    location = die.attributes.get("DW_AT_data_member_location")
    if location is None:
        return 0
    if not isinstance(location.value, int):
        raise _Rejection(member, "a member offset as a DWARF expression")
    return location.value


class _Flattener:
    """Walks one type and collects its leaves as Fields, in DWARF order."""

    def __init__(self):
        self.fields: list[Field] = []

    def struct(self, die, path: list[str], member: str, offset: int) -> None:
        for child in die.iter_children():
            if child.tag != "DW_TAG_member":
                continue
            name = _name(child)
            child_member = _join(member, name) if name else member
            child_path = path + [name] if name else path
            child_offset = offset + _member_offset(child, child_member or "?")
            self.value(_type_of(child), child_path, child_member,
                       child_offset)

    def value(self, die, path: list[str], member: str, offset: int) -> None:
        die = _unqualified(die)
        if die.tag in _REJECTED_TAGS:
            raise _Rejection(member, _REJECTED_TAGS[die.tag])
        if die.tag == "DW_TAG_structure_type":
            self.struct(die, path, member, offset)
        elif die.tag == "DW_TAG_array_type":
            self.array(die, path, member, offset)
        elif die.tag in _PRIMITIVE_TAGS:
            self.leaf(die, path, member, offset, None)
        else:
            raise _Rejection(member, die.tag)

    def array(self, die, path: list[str], member: str, offset: int) -> None:
        if "DW_AT_GNU_vector" in die.attributes:
            raise _Rejection(member, "vector type")
        dims = _dimensions(die, member)
        element = _unqualified(_type_of(die))
        # A typedef of an array adds its dimensions to the outer ones.
        while element.tag == "DW_TAG_array_type":
            if "DW_AT_GNU_vector" in element.attributes:
                raise _Rejection(member, "vector type")
            dims += _dimensions(element, member)
            element = _unqualified(_type_of(element))
        if element.tag in _PRIMITIVE_TAGS:
            self.leaf(element, path, member, offset, prod(dims))
            return
        stride = _byte_size(element)
        for flat in range(prod(dims)):
            index = _unravel(flat, dims)
            self.value(element, path + [str(i) for i in index],
                       member + "".join(f"[{i}]" for i in index),
                       offset + flat * stride)

    def leaf(self, die, path: list[str], member: str, offset: int,
             count: int | None) -> None:
        if not path:
            raise _Rejection(member, "a type that is not a struct")
        type_ = _primitive(die, member)
        size = _byte_size(die) * (count or 1)
        self.fields.append(
            Field("_".join(path), type_, count, offset, size, member))


def _join(member: str, name: str) -> str:
    return f"{member}.{name}" if member else name


def _unravel(flat: int, dims: list[int]) -> list[int]:
    index = []
    for dim in reversed(dims):
        flat, i = divmod(flat, dim)
        index.append(i)
    return index[::-1]


def _with_padding(fields: list[Field], size: int) -> list[Field]:
    """`fields` in offset order, each gap filled with a u8 padding field."""
    out: list[Field] = []
    cursor = 0
    for f in sorted(fields, key=lambda f: f.offset):
        if f.offset > cursor:
            out.append(_pad(cursor, f.offset - cursor))
        out.append(f)
        cursor = f.offset + f.size
    if size > cursor:
        out.append(_pad(cursor, size - cursor))
    return out


def _pad(offset: int, count: int) -> Field:
    return Field(f"{PAD_PREFIX}{offset}", "u8", count, offset, count, None)


def _name_problems(c_type: str, fields: list[Field]) -> list[str]:
    problems = []
    by_name = defaultdict(list)
    for f in fields:
        by_name[f.name].append(f)
    for name, same in by_name.items():
        if len(same) > 1:
            members = " and ".join(repr(f.member or name) for f in same)
            problems.append(f"type {c_type!r} members {members} both flatten "
                            f"to {name!r}")
        elif problem := ident_problem(name):
            problems.append(f"type {c_type!r} member {same[0].member!r}: "
                            f"field name {name!r} {problem}")
    return problems


def _incomplete(die) -> bool:
    """A declaration, or a typedef of one: no layout in this unit."""
    if die.tag == "DW_TAG_typedef":
        die = _unqualified(die)
    return "DW_AT_declaration" in die.attributes


class _TypeIndex:
    """Complete struct definitions and typedefs, by name, first one wins."""

    def __init__(self, dwarf):
        """`dwarf` is None for an object without debug information."""
        self.typedefs = {}
        self.structs = {}
        for cu in dwarf.iter_CUs() if dwarf else ():
            for die in cu.get_top_DIE().iter_children():
                name = _name(die)
                if name is None or _incomplete(die):
                    continue
                if die.tag == "DW_TAG_typedef":
                    self.typedefs.setdefault(name, die)
                elif die.tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
                    self.structs.setdefault(name, die)

    def find(self, name: str):
        """The DIE and its C spelling, or (None, None)."""
        if name in self.typedefs:
            return self.typedefs[name], name
        if name in self.structs:
            return self.structs[name], f"struct {name}"
        return None, None


def _layout(index: _TypeIndex, c_name: str, schema_name: str) -> Layout:
    die, spelling = index.find(c_name)
    if die is None:
        raise ImportRejected([
            f"type {c_name!r}: no DWARF type of this name (is the object "
            f"stripped, built without -g, or does no object use the type?)"])
    problems = []
    if problem := schema_name_problem(schema_name):
        problems.append(f"type {c_name!r}: Schema name {schema_name!r} "
                        f"{problem}")
    flattener = _Flattener()
    try:
        flattener.value(die, [], "", 0)
    except _Rejection as r:
        where = f" member {r.member!r}" if r.member else ""
        raise ImportRejected(
            problems + [f"type {c_name!r}{where}: {r.construct} is not "
                        f"supported"]) from None
    if not flattener.fields:
        raise ImportRejected(problems + [f"type {c_name!r}: the type has no "
                                         f"fields"])
    size = _byte_size(_unqualified(die))
    fields = _with_padding(flattener.fields, size)
    problems += _name_problems(c_name, fields)
    if problems:
        raise ImportRejected(problems)
    return Layout(spelling, schema_name, size, fields)


def import_layouts(elf_path: Path,
                   types: list[tuple[str, str]]) -> list[Layout]:
    """One Layout per (C type name, Schema name), in the given order."""
    from elftools.common.exceptions import ELFError
    from elftools.elf.elffile import ELFFile

    with open(elf_path, "rb") as stream:
        try:
            elf = ELFFile(stream)
        except ELFError as e:
            raise ImportRejected([f"{elf_path}: not an ELF object ({e})"])
        if not elf.little_endian:
            raise ImportRejected([
                f"{elf_path}: the object is big-endian; a Schema is "
                f"little-endian"])
        index = _TypeIndex(elf.get_dwarf_info() if elf.has_dwarf_info()
                           else None)
        layouts, problems = [], []
        for c_name, schema_name in types:
            try:
                layouts.append(_layout(index, c_name, schema_name))
            except ImportRejected as e:
                problems += e.problems
    if problems:
        raise ImportRejected(problems)
    return layouts


def schemas(layouts: list[Layout]) -> dict:
    """The Schema file content: each Schema's fields in offset order."""
    return {layout.schema_name: {"fields": [f.to_schema()
                                            for f in layout.fields]}
            for layout in layouts}


def layout_check(layouts: list[Layout]) -> str:
    """A C11 and C++17 header that checks each imported size and offset."""
    lines = [
        "/* Generated by sil-schema-import - do not edit.",
        " * Include the header that declares the imported types first. It",
        " * fails the build when a type no longer has the imported layout. */",
        "#pragma once",
        "",
        "#include <stddef.h>",
        "",
        "#ifdef __cplusplus",
        "#define SIL_LAYOUT_CHECK static_assert",
        "#else",
        "#define SIL_LAYOUT_CHECK _Static_assert",
        "#endif",
    ]
    for layout in layouts:
        t = layout.c_type
        lines += ["", f"/* {layout.schema_name} */",
                  f'SIL_LAYOUT_CHECK(sizeof({t}) == {layout.size}, '
                  f'"{layout.schema_name}: size of {t}");']
        for f in layout.fields:
            if f.member is None:
                continue
            lines += [
                f"SIL_LAYOUT_CHECK(offsetof({t}, {f.member}) == {f.offset}, "
                f'"{layout.schema_name}: offset of {f.member}");',
                f"SIL_LAYOUT_CHECK(sizeof((({t} *)0)->{f.member}) == "
                f'{f.size}, "{layout.schema_name}: size of {f.member}");',
            ]
    lines += ["", "#undef SIL_LAYOUT_CHECK", ""]
    return "\n".join(lines)


def _write_atomically(out: Path, text: str) -> None:
    """Replace `out` in one step, so no partial file is ever visible."""
    out.parent.mkdir(parents=True, exist_ok=True)
    # A plain write, unlike mkstemp, gives the file the umask's file mode.
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _fail(*messages: str) -> NoReturn:
    for message in messages:
        print(f"sil-schema-import: {message}", file=sys.stderr)
    sys.exit(2)


def _type_argument(text: str) -> tuple[str, str]:
    c_name, sep, schema_name = text.partition("=")
    if not sep or not c_name or not schema_name:
        raise argparse.ArgumentTypeError(
            f"expected <C type>=<Schema name>, got {text!r}")
    return c_name, schema_name


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="sil-schema-import",
        description="Import flat SiL Schemas from the DWARF layout of C "
                    "types in an ELF object.")
    parser.add_argument("elf", type=Path, help="ELF object (.o or .so) "
                        "with DWARF type information")
    parser.add_argument("--type", dest="types", action="append",
                        required=True, type=_type_argument,
                        metavar="CTYPE=SCHEMA",
                        help="a C struct tag or typedef name and its Schema "
                             "name; repeat for each type")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="the Schema file to write")
    parser.add_argument("--layout-check", type=Path, required=True,
                        help="the C layout-check header to write")
    args = parser.parse_args(argv)

    try:
        import elftools  # noqa: F401
    except ImportError:
        _fail("reading DWARF needs pyelftools; install it with "
              "pip install 'sil[dwarf]'")
    requested = Counter(schema_name for _, schema_name in args.types)
    duplicates = sorted(s for s, n in requested.items() if n > 1)
    if duplicates:
        _fail(*(f"Schema {s!r} is requested more than once"
                for s in duplicates))
    try:
        layouts = import_layouts(args.elf, args.types)
    except OSError as e:
        _fail(f"cannot read {args.elf}: {e}")
    except ImportRejected as e:
        _fail(*e.problems)
    _write_atomically(args.output, json.dumps(schemas(layouts), indent=2)
                      + "\n")
    _write_atomically(args.layout_check, layout_check(layouts))
    for layout in layouts:
        print(f"{layout.schema_name}: {len(layout.fields)} fields, "
              f"{layout.size} bytes")


if __name__ == "__main__":
    main()
