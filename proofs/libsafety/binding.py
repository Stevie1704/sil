"""opendbc's safety library, bound with ctypes for the SiL adapter.

This file knows the library and nothing about the Step protocol. It declares
every symbol the adapter calls, with the C types of opendbc's own libsafety
harness declarations (`opendbc/safety/tests/libsafety/libsafety_py.py` at the
pinned commit), and maps the library's lifecycle onto these calls:

- `Libsafety(library)` resolves every symbol, so a missing one fails before
  anything runs;
- `init(mode, param, alternative_experience)` selects the safety hooks and
  must be accepted with 0, then sets the alternative experience;
- `set_timer(us)`, `tick()`, `forward(bus, address)`,
  `receive(address, bus, data)` and `config_valid()` are one event's calls;
- `state()` reads the observed state fields.

There is no shutdown call: the library ends with its process.

The runtime image has no CFFI and no opendbc, so `packet` builds the packed
`CANPacket_t` bytes itself. Preparation compares them with upstream's
`make_CANPacket` for every recorded frame.
"""

from __future__ import annotations

import ctypes

# opendbc's packed CANPacket_t: a 40-bit header, a checksum byte, 64 data bytes.
PACKET_BYTES = 70
DATA_BYTES = 64
_HEADER_BYTES = 5
# Standard identifiers are 11 bits; a larger address is an extended frame.
_EXTENDED_FROM = 0x800
# Classic CAN: the data length code of 0 to 8 bytes is the length itself.
_MAX_CLASSIC_LENGTH = 8

STATE = {
    "controls_allowed": ctypes.c_bool,
    "gas_pressed_prev": ctypes.c_bool,
    "brake_pressed_prev": ctypes.c_bool,
    "cruise_engaged_prev": ctypes.c_bool,
    "vehicle_moving": ctypes.c_bool,
    "acc_main_on": ctypes.c_bool,
    "vehicle_speed_min": ctypes.c_float,
    "vehicle_speed_max": ctypes.c_float,
}


class BindingError(Exception):
    """The library refused a call, or does not export one."""


def packet(address: int, bus: int, data: bytes) -> bytes:
    """The packed CANPacket_t upstream's `make_CANPacket` builds.

    Header bits, least significant first: fd 1, bus 3, data_len_code 4,
    rejected 1, returned 1, extended 1, addr 29. The checksum byte stays 0,
    as upstream leaves it."""
    if len(data) > _MAX_CLASSIC_LENGTH:
        raise BindingError(f"a {len(data)}-byte payload is not classic CAN")
    header = ((bus & 0x7) << 1) | (len(data) << 4) | (address << 11)
    if address >= _EXTENDED_FROM:
        header |= 1 << 10
    return (header.to_bytes(_HEADER_BYTES, "little") + b"\x00"
            + data.ljust(DATA_BYTES, b"\x00"))


class Libsafety:
    """The library's C API, bound for the adapter."""

    def __init__(self, library: ctypes.CDLL):
        self._set_safety_hooks = _function(
            library, "set_safety_hooks", ctypes.c_int,
            ctypes.c_uint16, ctypes.c_uint16)
        self._set_alternative_experience = _function(
            library, "set_alternative_experience", None, ctypes.c_int)
        self._set_timer = _function(library, "set_timer", None, ctypes.c_uint32)
        self._safety_tick = _function(library, "safety_tick", None)
        self._safety_fwd_hook = _function(
            library, "safety_fwd_hook", ctypes.c_int, ctypes.c_int, ctypes.c_int)
        self._safety_rx_hook = _function(
            library, "safety_rx_hook", ctypes.c_bool, ctypes.c_char_p)
        self._safety_config_valid = _function(
            library, "safety_config_valid", ctypes.c_bool)
        self._getters = {name: _function(library, f"get_{name}", restype)
                         for name, restype in STATE.items()}

    def init(self, mode: int, param: int, alternative_experience: int) -> None:
        status = self._set_safety_hooks(mode, param)
        if status != 0:
            raise BindingError(
                f"set_safety_hooks({mode}, {param}) returned {status}, not 0")
        self._set_alternative_experience(alternative_experience)

    def set_timer(self, microseconds: int) -> None:
        self._set_timer(microseconds)

    def tick(self) -> None:
        self._safety_tick()

    def forward(self, bus: int, address: int) -> None:
        self._safety_fwd_hook(bus, address)

    def receive(self, address: int, bus: int, data: bytes) -> bool:
        return self._safety_rx_hook(packet(address, bus, data))

    def config_valid(self) -> bool:
        return self._safety_config_valid()

    def state(self) -> dict:
        return {name: getter() for name, getter in self._getters.items()}


def _function(library: ctypes.CDLL, name: str, restype, *argtypes):
    try:
        function = getattr(library, name)
    except AttributeError as error:
        raise BindingError(
            f"does not export {name!r}, which this binding calls") from error
    function.restype = restype
    function.argtypes = list(argtypes)
    return function
