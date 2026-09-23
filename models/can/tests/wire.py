"""Independent Classical CAN wire-length reference for the timing tests.

Deliberately written differently from `src/bus.cpp`: the CRC is the remainder
of a polynomial division on Python integers, not a shift register, and bit
stuffing is applied to a string of '0'/'1' characters. Agreement between the
two is evidence, not a shared implementation.
"""

# x^15 + x^14 + x^10 + x^8 + x^7 + x^4 + x^3 + 1 (ISO 11898-1 CRC-15).
CRC15 = 0xC599
# CRC delimiter, ACK slot, ACK delimiter and seven EOF bits; never stuffed.
TRAILER = 1 + 1 + 1 + 7
INTERMISSION = 3


def unstuffed(identifier: int, payload: bytes) -> str:
    """SOF through data field of an 11-bit data frame: IDE = RTR = r0 = 0."""
    header = "0" + f"{identifier:011b}" + "000" + f"{len(payload):04b}"
    return header + "".join(f"{byte:08b}" for byte in payload)


def crc15(bits: str) -> str:
    remainder = int(bits, 2) << 15
    while remainder.bit_length() > 15:
        remainder ^= CRC15 << (remainder.bit_length() - 16)
    return f"{remainder:015b}"


def stuff(bits: str) -> str:
    """Insert a complement after every five equal bits, stuff bits included."""
    out = ""
    for bit in bits:
        out += bit
        if out.endswith("00000"):
            out += "1"
        elif out.endswith("11111"):
            out += "0"
    return out


def frame_bits(identifier: int, payload: bytes) -> int:
    """Bits from SOF through the last EOF bit, with exact stuffing."""
    body = unstuffed(identifier, payload)
    return len(stuff(body + crc15(body))) + TRAILER
