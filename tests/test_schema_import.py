"""sil-schema-import: a flat Schema from the DWARF layout of a C type (#253).

The fixture types in tests/fixtures/dwarf_layout.h are compiled to an ELF
object and imported. The tests prove the import by running code: a value of
each original type, copied with memcpy into the struct that silschema
generates from the import, decodes through sil.schema to the value of each
member read through the original type. On a host that does not build ELF
objects, clang cross-compiles them for x86-64 Linux, whose layout of the
fixture types is the same as the host's.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT

from sil import schema
from sil.manifest import Manifest
from sil.testing import run_simulation

pytest.importorskip("elftools", reason="needs the sil[dwarf] extra")

FIXTURES = ROOT / "tests" / "fixtures"
TYPES = ["AdasInput=adas.Input", "AdasOutput=adas.Output"]
MS = 1_000_000


def elf_compiler() -> list[str]:
    if sys.platform.startswith("linux"):
        return ["cc"]
    if shutil.which("clang") is None:
        pytest.skip("needs clang to build an ELF object on this host")
    return ["clang", "--target=x86_64-linux-gnu", "-ffreestanding"]


def compile_elf(source: Path, out: Path, *flags: str) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [*elf_compiler(), "-std=c11", "-c", *flags, "-I", str(FIXTURES),
         str(source), "-o", str(out)],
        check=True, capture_output=True, text=True,
    )
    return out


def run_import(obj: Path, out_dir: Path, *types: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    args = [arg for t in types for arg in ("--type", t)]
    proc = subprocess.run(
        [sys.executable, "-m", "sil.schema_import", str(obj), *args,
         "-o", str(out_dir / "schemas.json"),
         "--layout-check", str(out_dir / "layout_check.h")],
        capture_output=True, text=True,
    )
    proc.schemas = out_dir / "schemas.json"
    proc.layout_check = out_dir / "layout_check.h"
    return proc


def imported(tmp_path: Path, *flags: str) -> subprocess.CompletedProcess:
    obj = compile_elf(FIXTURES / "dwarf_layout.c", tmp_path / "layout.o",
                      *(flags or ("-O2", "-g")))
    proc = run_import(obj, tmp_path / "import", *TYPES)
    assert proc.returncode == 0, proc.stderr
    return proc


def generate_header(schemas: Path, out_dir: Path) -> Path:
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "silschema.py"), str(schemas),
         str(out_dir / "dwarf_messages.h")],
        check=True, capture_output=True, text=True,
    )
    return out_dir


def pad(offset: int, count: int) -> dict:
    return {"name": f"_sil_pad_{offset}", "type": "u8", "count": count}


def field(name: str, type_: str, count: int | None = None) -> dict:
    return {"name": name, "type": type_} | ({"count": count} if count else {})


OBJECT_FIELDS = [
    f
    for i in range(3)
    for f in (
        field(f"objects_{i}_id", "u16"),
        field(f"objects_{i}_cls", "i8"),
        field(f"objects_{i}_lateral", "i8"),
        field(f"objects_{i}_lane", "u8"),
        pad(64 + 12 * i + 5, 3),
        field(f"objects_{i}_range", "f32"),
    )
]
EXPECTED = {
    "adas.Input": {"fields": [
        field("seq", "u8"),
        pad(1, 7),
        field("ego_pose_x", "f32"),
        field("ego_pose_y", "f32"),
        field("ego_pose_yaw", "f64"),
        field("ego_odometer", "f64"),
        field("ego_valid", "u8"),
        pad(33, 7),
        field("grid", "f32", 6),
        *OBJECT_FIELDS,
        field("gear", "u32"),
        field("brake", "u8"),
        pad(105, 7),
    ]},
    "adas.Output": {"fields": [
        field("accel_cmd", "i16"),
        pad(2, 2),
        field("status", "u32"),
        field("offset", "i32"),
        pad(12, 4),
        field("counters", "i64", 2),
    ]},
}


@pytest.fixture(scope="module")
def decoded(tmp_path_factory) -> dict:
    tmp_path = tmp_path_factory.mktemp("memcpy")
    proc = imported(tmp_path)
    include = generate_header(proc.schemas, tmp_path)
    exe = tmp_path / "decode"
    subprocess.run(
        ["cc", "-std=c11", "-O2", "-I", str(FIXTURES), "-I", str(include),
         str(FIXTURES / "dwarf_layout_decode.c"), "-o", str(exe)],
        check=True, capture_output=True, text=True,
    )
    lines = subprocess.run([str(exe)], check=True, capture_output=True,
                           text=True).stdout.splitlines()
    return {
        "schemas": schema.load(json.loads(proc.schemas.read_text())),
        "lines": [line.split() for line in lines],
    }


@pytest.fixture(scope="module")
def rejected_object(tmp_path_factory) -> Path:
    tmp_path = tmp_path_factory.mktemp("rejected")
    return compile_elf(FIXTURES / "dwarf_rejected.c",
                       tmp_path / "rejected.o", "-O2", "-g")


@pytest.fixture(scope="module")
def publisher(tmp_path_factory) -> tuple[Path, dict]:
    tmp_path = tmp_path_factory.mktemp("publisher")
    proc = imported(tmp_path)
    include = generate_header(proc.schemas, tmp_path)
    library = tmp_path / "dwarf_publisher.so"
    subprocess.run(
        ["cc", "-std=c11", "-shared", "-fPIC", "-O2",
         "-I", str(ROOT / "include"), "-I", str(FIXTURES),
         "-I", str(include),
         str(FIXTURES / "participants" / "dwarf_publisher.c"),
         "-o", str(library)],
        check=True, capture_output=True, text=True,
    )
    return library, json.loads(proc.schemas.read_text())


class TestFlatSchema:
    def test_each_rule_flattens_the_fixture_in_offset_order(self, tmp_path):
        proc = imported(tmp_path)
        assert json.loads(proc.schemas.read_text()) == EXPECTED

    def test_optimized_and_unoptimized_builds_import_identically(
        self, tmp_path
    ):
        o0 = imported(tmp_path / "o0", "-O0", "-g")
        o2 = imported(tmp_path / "o2", "-O2", "-g")
        assert o0.schemas.read_bytes() == o2.schemas.read_bytes()
        assert o0.layout_check.read_bytes() == o2.layout_check.read_bytes()

    def test_repeated_imports_are_byte_identical(self, tmp_path):
        obj = compile_elf(FIXTURES / "dwarf_layout.c", tmp_path / "layout.o",
                          "-O2", "-g")
        first = run_import(obj, tmp_path / "first", *TYPES)
        second = run_import(obj, tmp_path / "second", *TYPES)
        assert first.returncode == second.returncode == 0
        assert first.schemas.read_bytes() == second.schemas.read_bytes()
        assert first.layout_check.read_bytes() == (
            second.layout_check.read_bytes())

    def test_prints_the_field_count_and_byte_size_of_each_schema(
        self, tmp_path
    ):
        proc = imported(tmp_path)
        assert proc.stdout.splitlines() == [
            "adas.Input: 30 fields, 112 bytes",
            "adas.Output: 6 fields, 32 bytes",
        ]


class TestMemcpy:
    def test_the_sil_struct_has_the_size_of_the_original_type(self, decoded):
        sizes = {s: (int(a), int(b)) for kind, s, a, b in
                 (line for line in decoded["lines"] if line[0] == "size")}
        assert sizes == {"adas.Input": (112, 112), "adas.Output": (32, 32)}

    def test_each_member_decodes_to_the_value_of_the_original_type(
        self, decoded
    ):
        values = {
            s: decoded["schemas"][s].unpack(bytes.fromhex(hexbytes))
            for kind, s, hexbytes in
            (line for line in decoded["lines"] if line[0] == "bytes")
        }
        members = [line for line in decoded["lines"] if line[0] == "member"]
        assert len(members) == 29 + 5
        for _, s, name, index, text in members:
            value = values[s][name]
            if index != "-":
                value = value[int(index)]
            expected = float.fromhex(text) if "p" in text else int(text)
            assert value == expected, f"{s} {name}[{index}]"

    def test_fill_bytes_reach_each_member_unchanged(self, decoded):
        # The distinct fill bytes make a misplaced offset visible: x is the
        # little-endian word of bytes 9..12, the first after seq's padding.
        [line] = [line for line in decoded["lines"]
                  if line[:3] == ["member", "adas.Input", "ego_pose_x"]]
        assert float.fromhex(line[4]) == struct.unpack(
            "<f", bytes([9, 10, 11, 12]))[0]


class TestLayoutCheck:
    def compile_check(self, tmp_path: Path, header: Path, language: str):
        source = tmp_path / f"check.{'c' if language == 'c' else 'cpp'}"
        source.write_text(
            f'#include "{header.name}"\n#include "layout_check.h"\n')
        compiler = (["cc", "-std=c11"] if language == "c"
                    else ["c++", "-std=c++17"])
        return subprocess.run(
            [*compiler, "-fsyntax-only", "-I", str(header.parent),
             "-I", str(tmp_path / "import"), str(source)],
            capture_output=True, text=True,
        )

    @pytest.mark.parametrize("language", ["c", "c++"])
    def test_compiles_against_the_fixture(self, tmp_path, language):
        imported(tmp_path)
        proc = self.compile_check(tmp_path, FIXTURES / "dwarf_layout.h",
                                  language)
        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize("language", ["c", "c++"])
    @pytest.mark.parametrize(
        ("original", "changed", "member"),
        [
            ("  float x;\n  float y;\n", "  float y;\n  float x;\n",
             "ego.pose.x"),
            ("  float range;", "  double range;", "objects[0].range"),
        ],
        ids=["moved", "resized"],
    )
    def test_fails_the_build_when_a_member_changes(
        self, tmp_path, language, original, changed, member
    ):
        imported(tmp_path)
        drifted = tmp_path / "drifted" / "dwarf_layout.h"
        drifted.parent.mkdir()
        text = (FIXTURES / "dwarf_layout.h").read_text()
        assert original in text
        drifted.write_text(text.replace(original, changed))
        proc = self.compile_check(tmp_path, drifted, language)
        assert proc.returncode != 0
        assert member in proc.stderr


class TestRejected:
    @pytest.mark.parametrize(
        ("type_", "diagnostic"),
        [
            ("WithUnion", "type 'WithUnion' member 'payload.value': union"),
            ("WithBitField",
             "type 'WithBitField' member 'flags.mode': bit-field"),
            ("WithPointer",
             "type 'WithPointer' member 'buffer.samples': pointer"),
            ("WithFlexibleArray",
             "type 'WithFlexibleArray' member 'samples': flexible array"),
            ("WithCollision",
             "type 'WithCollision' members 'a_b.c' and 'a.b_c' both flatten "
             "to 'a_b_c'"),
            ("WithReservedName",
             "type 'WithReservedName' member 'header.len_t': field name "
             "'header_len_t' ends in '_t'"),
            ("WithLongDouble",
             "type 'WithLongDouble' member 'precise': long double"),
            ("Absent", "type 'Absent': no DWARF type"),
        ],
    )
    def test_is_exit_2_with_the_type_and_member_path(
        self, rejected_object, tmp_path, type_, diagnostic
    ):
        proc = run_import(rejected_object, tmp_path, f"{type_}=x.Y")
        assert proc.returncode == 2
        assert diagnostic in proc.stderr
        assert list(tmp_path.iterdir()) == []

    def test_a_stripped_object_is_rejected(self, tmp_path):
        obj = compile_elf(FIXTURES / "dwarf_layout.c", tmp_path / "layout.o",
                          "-O2", "-g")
        stripper = shutil.which("objcopy") or shutil.which("llvm-objcopy")
        if stripper:
            subprocess.run([stripper, "--strip-debug", str(obj)], check=True)
        else:  # the same object without its debug sections
            compile_elf(FIXTURES / "dwarf_layout.c", obj, "-O2")
        proc = run_import(obj, tmp_path / "import", "AdasInput=adas.Input")
        assert proc.returncode == 2
        assert "type 'AdasInput': no DWARF type" in proc.stderr
        assert not (tmp_path / "import" / "schemas.json").exists()

    def test_a_big_endian_object_is_rejected(self, tmp_path):
        if shutil.which("clang") is None:
            pytest.skip("needs clang to build a big-endian object")
        obj = tmp_path / "be.o"
        subprocess.run(
            ["clang", "--target=aarch64_be-linux-gnu", "-ffreestanding",
             "-std=c11", "-g", "-c", "-I", str(FIXTURES),
             str(FIXTURES / "dwarf_layout.c"), "-o", str(obj)],
            check=True, capture_output=True, text=True,
        )
        proc = run_import(obj, tmp_path / "import", "AdasInput=adas.Input")
        assert proc.returncode == 2
        assert "big-endian" in proc.stderr

    def test_a_missing_extra_names_the_install_command(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.modules['elftools'] = None; "
             "from sil.schema_import import main; main()",
             "x.o", "--type", "A=a.A", "-o", str(tmp_path / "s.json"),
             "--layout-check", str(tmp_path / "c.h")],
            capture_output=True, text=True,
        )
        assert proc.returncode == 2
        assert "sil[dwarf]" in proc.stderr


class TestRun:
    """The imported Schema carries the project struct through a real Run."""

    def run(self, sil_run, publisher, workdir: Path, **override):
        library, schemas = publisher
        m = Manifest(duration_ns=30 * MS)
        m.add_schemas(schemas)
        m.add_channel("adas.input", schema="adas.Input")
        m.add_native("publisher", library=str(library),
                     publishes=["adas.input"])
        if override:
            m.add_interceptor("adas.input", kind="override", **override)
        workdir.mkdir()
        return run_simulation(m, runner=sil_run, workdir=workdir).messages(
            "adas.input")

    def test_the_recording_decodes_to_the_member_values(
        self, sil_run, publisher, tmp_path
    ):
        messages = self.run(sil_run, publisher, tmp_path / "run")
        assert [t for t, _ in messages] == [0, 10 * MS, 20 * MS]
        _, second = messages[1]
        assert {k: v for k, v in second.items()
                if not k.startswith("_sil_pad_")} == {
            "seq": 1,
            "ego_pose_x": 2.5, "ego_pose_y": -2.25, "ego_pose_yaw": 0.125,
            "ego_odometer": 101.0, "ego_valid": 1,
            "grid": [0.0, 1.0, 2.0, 10.0, 11.0, 12.0],
            **{k: v for i in range(3) for k, v in {
                f"objects_{i}_id": 100 + i,
                f"objects_{i}_cls": ord("a") + i,
                f"objects_{i}_lateral": -1 - i,
                f"objects_{i}_lane": i,
                f"objects_{i}_range": 10.0 * (i + 1),
            }.items()},
            "gear": 3, "brake": 1,
        }

    def test_an_override_changes_only_the_unrolled_element(
        self, sil_run, publisher, tmp_path
    ):
        plain = self.run(sil_run, publisher, tmp_path / "plain")
        overridden = self.run(sil_run, publisher, tmp_path / "override",
                              field="objects_1_range", value=99.5)
        assert len(plain) == len(overridden) == 3
        for (_, before), (_, after) in zip(plain, overridden):
            assert after["objects_1_range"] == 99.5
            assert {k: v for k, v in after.items() if k != "objects_1_range"} \
                == {k: v for k, v in before.items() if k != "objects_1_range"}
