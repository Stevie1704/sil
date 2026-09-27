"""The CANPacket_t bytes the binding hands to safety_rx_hook.

The expected bytes are worked out by hand from opendbc's packed declaration:
fd:1, bus:3, data_len_code:4, rejected:1, returned:1, extended:1, addr:29,
then checksum and 64 data bytes. Preparation checks the same function against
upstream's own CFFI `make_CANPacket` for every recorded frame.
"""
from binding import PACKET_BYTES, packet


def test_a_standard_frame_packs_bus_length_and_address():
    raw = packet(0x260, 1, bytes([1, 2, 3, 4, 5, 6, 7, 8]))
    # 0x260 << 11 = 0x130000; bus 1 << 1 = 0x2; length 8 << 4 = 0x80
    assert raw[:5] == bytes([0x82, 0x00, 0x13, 0x00, 0x00])
    assert raw[5] == 0
    assert raw[6:14] == bytes([1, 2, 3, 4, 5, 6, 7, 8])
    assert raw[14:] == bytes(56)
    assert len(raw) == PACKET_BYTES == 70


def test_an_address_above_0x7ff_is_marked_extended():
    raw = packet(0x18DAF110, 2, b"\x01")
    header = int.from_bytes(raw[:5], "little")
    assert header == (0x18DAF110 << 11) | (1 << 10) | (1 << 4) | (2 << 1)
