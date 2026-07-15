"""Runtime pack/unpack contract for sil.schema, including fixed-size arrays.

The byte layout is the shared wire contract (little-endian, declared field
order, no padding) that C participants pack to via tools/silschema.py. These
tests pin that layout so a Python-packed message is byte-identical to the C
struct — the property the inline transport relies on.
"""

import struct

import pytest

from sil import schema

COUNTER = {"fields": [{"name": "seq", "type": "u64"}, {"name": "value", "type": "i64"}]}


class TestScalarUnchanged:
    def test_pack_unpack_roundtrip(self):
        [mt] = schema.load({"toy.Counter": COUNTER}).values()
        data = mt.pack(seq=7, value=-3)
        assert data == struct.pack("<Qq", 7, -3)
        assert mt.unpack(data) == {"seq": 7, "value": -3}
        assert mt.size == 16


class TestArrayLayout:
    def _payload_type(self):
        spec = {
            "fields": [
                {"name": "id", "type": "u32"},
                {"name": "samples", "type": "f32", "count": 4},
            ]
        }
        return schema.load({"big.Payload": spec})["big.Payload"]

    def test_size_includes_array_bytes(self):
        # u32 (4) + 4 * f32 (16) = 20 bytes, no padding.
        assert self._payload_type().size == 20

    def test_list_field_packs_in_declared_order(self):
        mt = self._payload_type()
        data = mt.pack(id=9, samples=[1.0, 2.0, 3.0, 4.0])
        assert data == struct.pack("<Iffff", 9, 1.0, 2.0, 3.0, 4.0)

    def test_roundtrip_returns_list_for_non_u8_arrays(self):
        mt = self._payload_type()
        out = mt.unpack(mt.pack(id=1, samples=[10.0, 20.0, 30.0, 40.0]))
        assert out == {"id": 1, "samples": [10.0, 20.0, 30.0, 40.0]}
        assert isinstance(out["samples"], list)

    def test_u8_array_surfaces_as_bytes(self):
        spec = {"fields": [{"name": "blob", "type": "u8", "count": 3}]}
        mt = schema.load({"S": spec})["S"]
        data = mt.pack(blob=b"\x01\x02\x03")
        assert data == b"\x01\x02\x03"
        out = mt.unpack(data)
        assert out == {"blob": b"\x01\x02\x03"}
        assert isinstance(out["blob"], bytes)

    def test_wrong_array_length_is_rejected_on_pack(self):
        mt = self._payload_type()
        with pytest.raises(struct.error):
            mt.pack(id=1, samples=[1.0, 2.0])

    def test_wrong_u8_array_length_is_rejected_not_silently_padded(self):
        # struct's 'Ns' would NUL-pad/truncate; a wrong-size blob must raise so
        # it can never corrupt the bit-for-bit payload.
        spec = {"fields": [{"name": "blob", "type": "u8", "count": 3}]}
        mt = schema.load({"S": spec})["S"]
        with pytest.raises(struct.error):
            mt.pack(blob=b"\x01\x02")
        with pytest.raises(struct.error):
            mt.pack(blob=b"\x01\x02\x03\x04")

    def test_mixed_scalar_and_array_fields(self):
        spec = {
            "fields": [
                {"name": "flags", "type": "u8", "count": 2},
                {"name": "n", "type": "u16"},
                {"name": "coords", "type": "i32", "count": 3},
            ]
        }
        mt = schema.load({"M": spec})["M"]
        data = mt.pack(flags=b"\xaa\xbb", n=500, coords=[-1, 0, 1])
        assert data == struct.pack("<2BHiii", 0xAA, 0xBB, 500, -1, 0, 1)
        assert mt.unpack(data) == {
            "flags": b"\xaa\xbb",
            "n": 500,
            "coords": [-1, 0, 1],
        }
