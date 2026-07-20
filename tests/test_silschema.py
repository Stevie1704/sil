"""C header generation from silschema.py, focused on fixed-size arrays.

The generated struct and its packed-layout static_assert are the C side of
the shared wire contract; they must agree byte-for-byte with sil.schema.
"""

import importlib.util
from pathlib import Path

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
