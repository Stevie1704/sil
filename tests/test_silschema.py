"""C header generation from silschema.py.

The generated struct and its packed-layout size check are the C side of the
shared wire contract; they must agree byte-for-byte with sil.schema. The
tool refuses names it cannot turn into a valid C11 and C++17 header.
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "silschema", ROOT / "tools" / "silschema.py"
)
silschema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(silschema)


def test_scalar_struct_unchanged():
    out = silschema.generate(
        {"toy.Counter": {"fields": [{"name": "seq", "type": "u64"}]}}
    )
    assert "uint64_t seq;" in out
    assert "sizeof(toy_Counter) == 8" in out


def test_array_member_emitted_as_c_array():
    out = silschema.generate(
        {
            "big.Payload": {
                "fields": [
                    {"name": "id", "type": "u32"},
                    {"name": "samples", "type": "f32", "count": 4},
                ]
            }
        }
    )
    assert "uint32_t id;" in out
    assert "float samples[4];" in out


def test_static_assert_includes_array_bytes():
    out = silschema.generate(
        {
            "big.Payload": {
                "fields": [
                    {"name": "id", "type": "u32"},
                    {"name": "samples", "type": "f32", "count": 4},
                ]
            }
        }
    )
    # u32 (4) + 4 * f32 (16) = 20 bytes.
    assert "sizeof(big_Payload) == 20" in out


def test_u8_array_member():
    out = silschema.generate(
        {"S": {"fields": [{"name": "blob", "type": "u8", "count": 3}]}}
    )
    assert "uint8_t blob[3];" in out
    assert "sizeof(S) == 3" in out


def _write_schemas(tmp_path: Path, schemas: str) -> Path:
    path = tmp_path / "s.json"
    path.write_text(schemas)
    return path


def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "silschema.py"), *map(str, args)],
        capture_output=True,
        text=True,
    )


def test_issue_reproduction_is_rejected_without_output(tmp_path):
    src = _write_schemas(
        tmp_path,
        """{"t.S": {"fields": [{"name": "pose.x", "type": "f32"},
                              {"name": "int", "type": "u8"},
                              {"name": "a b", "type": "u8"}]},
           "t-1": {"fields": [{"name": "y", "type": "u8"}]},
           "t_1": {"fields": [{"name": "y", "type": "u8"}]}}""",
    )
    out = tmp_path / "s.h"
    result = _run(src, out)
    assert result.returncode == 2
    assert "Schema 't.S' field 'pose.x'" in result.stderr
    assert "Schema 't.S' field 'int'" in result.stderr
    assert "keyword" in result.stderr
    assert "Schema 't.S' field 'a b'" in result.stderr
    assert "Schema 't-1'" in result.stderr
    assert not out.exists()
    assert list(tmp_path.iterdir()) == [src]


def test_rejection_keeps_an_existing_output_unchanged(tmp_path):
    src = _write_schemas(tmp_path, '{"S": {"fields": [{"name": "int", "type": "u8"}]}}')
    out = tmp_path / "s.h"
    out.write_text("previous")
    assert _run(src, out).returncode == 2
    assert out.read_text() == "previous"


@pytest.mark.parametrize(
    ("name", "rule"),
    [
        ("pose.x", "[A-Za-z_][A-Za-z0-9_]*"),
        ("1x", "[A-Za-z_][A-Za-z0-9_]*"),
        ("", "[A-Za-z_][A-Za-z0-9_]*"),
        ("int", "keyword"),
        ("_Bool", "keyword"),
        ("class", "keyword"),
        ("static_assert", "keyword"),
        ("a__b", "'__'"),
        ("_Pose", "'_' followed by an uppercase letter"),
    ],
)
def test_invalid_field_name_names_schema_field_and_rule(name, rule):
    problems = silschema.validate({"t.S": {"fields": [{"name": name, "type": "u8"}]}})
    assert len(problems) == 1
    assert f"Schema 't.S' field '{name}'" in problems[0]
    assert rule in problems[0]


@pytest.mark.parametrize("name", ["t-1", "t..S", ".S", "t.", "t.int", "t._S", "a_.b"])
def test_invalid_schema_name_is_named(name):
    problems = silschema.validate({name: {"fields": [{"name": "y", "type": "u8"}]}})
    assert len(problems) == 1
    assert f"Schema '{name}'" in problems[0]


def test_duplicate_field_name_is_rejected():
    problems = silschema.validate(
        {"S": {"fields": [{"name": "y", "type": "u8"}, {"name": "y", "type": "u8"}]}}
    )
    assert problems == ["Schema 'S' field 'y': duplicate field name"]


def test_schema_names_that_map_to_one_c_identifier_are_both_named():
    problems = silschema.validate(
        {
            "a.b_c": {"fields": [{"name": "y", "type": "u8"}]},
            "a_b.c": {"fields": [{"name": "y", "type": "u8"}]},
        }
    )
    assert problems == [
        "Schemas 'a.b_c' and 'a_b.c' both map to the C identifier 'a_b_c'"
    ]


def test_valid_names_are_accepted():
    assert silschema.validate(
        {
            "acc.Sensing": {"fields": [{"name": "x_1", "type": "f64"}]},
            "_t.s_": {"fields": [{"name": "_x", "type": "u8"}]},
        }
    ) == []


def test_size_check_is_emitted_for_c_and_cpp():
    out = silschema.generate({"S": {"fields": [{"name": "y", "type": "u8"}]}})
    assert (
        "#ifdef __cplusplus\n"
        'static_assert(sizeof(S) == 1, "S layout must be packed");\n'
        "#else\n"
        '_Static_assert(sizeof(S) == 1, "S layout must be packed");\n'
        "#endif"
    ) in out


needs_cc = pytest.mark.skipif(
    shutil.which("cc") is None or shutil.which("c++") is None,
    reason="needs a C and a C++ compiler",
)

_COMPILERS = [("cc", "c", "-std=c11"), ("c++", "c++", "-std=c++17")]


def _syntax_check(header: Path, compiler: str, lang: str, std: str):
    """Compile a translation unit that includes `header`, as an adapter does."""
    unit = header.with_name("unit")
    unit.write_text(f'#include "{header.name}"\n')
    return subprocess.run(
        [compiler, "-fsyntax-only", "-x", lang, std, "-Wall", "-Werror", str(unit)],
        capture_output=True,
        text=True,
    )


_VALID = {
    "t.Pose": {
        "fields": [
            {"name": "x", "type": "f64"},
            {"name": "_id", "type": "u8"},
            {"name": "samples", "type": "f32", "count": 3},
        ]
    },
    "t.Empty_1": {"fields": [{"name": "y", "type": "i16"}]},
}


@needs_cc
@pytest.mark.parametrize(("compiler", "lang", "std"), _COMPILERS)
def test_valid_header_compiles(tmp_path, compiler, lang, std):
    header = tmp_path / "s.h"
    header.write_text(silschema.generate(_VALID))
    result = _syntax_check(header, compiler, lang, std)
    assert result.returncode == 0, result.stderr


@needs_cc
@pytest.mark.parametrize(("compiler", "lang", "std"), _COMPILERS)
def test_size_check_fires_for_a_wrong_size(tmp_path, compiler, lang, std):
    text = silschema.generate(_VALID)
    # 8 + 1 + 3 * 4 = 21 bytes; claim 22 instead.
    assert "sizeof(t_Pose) == 21" in text
    header = tmp_path / "s.h"
    header.write_text(text.replace("sizeof(t_Pose) == 21", "sizeof(t_Pose) == 22"))
    result = _syntax_check(header, compiler, lang, std)
    assert result.returncode != 0
    assert "t.Pose layout must be packed" in result.stderr


@pytest.mark.parametrize(
    "schemas", ['{"S": {}}', '{"S": {"fields": [{"type": "u8"}]}}', "[]"]
)
def test_malformed_schema_set_is_a_usage_error(tmp_path, schemas):
    result = _run(_write_schemas(tmp_path, schemas), tmp_path / "s.h")
    assert result.returncode == 2
    assert "is not a Schema set" in result.stderr
    assert not (tmp_path / "s.h").exists()


def test_output_file_mode_follows_the_umask(tmp_path):
    src = _write_schemas(tmp_path, '{"S": {"fields": [{"name": "y", "type": "u8"}]}}')
    out = tmp_path / "s.h"
    reference = tmp_path / "reference"
    reference.write_text("")
    assert _run(src, out).returncode == 0
    assert out.stat().st_mode == reference.stat().st_mode
