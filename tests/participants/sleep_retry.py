"""Toy retry-loop process participant for the sleep-policy suite (issue #52).

Each step it runs the pattern the reject policy exists to end: retry a short
sleep until it stops succeeding. Virtual time is frozen for the whole step, so
no amount of sleeping makes progress — under ``immediate`` every call succeeds
and the loop runs to its cap, and under ``reject`` the first call fails and it
leaves.

libc's ``nanosleep`` is called through ctypes rather than ``time.sleep``
because CPython's sleep is free to use a call the shim does not interpose
(``select`` on some platforms). This asserts the shim's contract, not
CPython's internals.

Published on toy.Counter: seq = iterations run, value = errno of the call that
ended the loop (0 when it ran to the cap).
"""

import ctypes

from sil.participant import StepParticipant, run

# Bounded so a shim regression fails the assertion instead of hanging the
# suite: under immediate this costs nothing (no call actually sleeps), and a
# shim that stopped interposing would cost CAP milliseconds per step.
CAP = 200
BACKOFF_NS = 1_000_000  # 1 ms


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_nsec", ctypes.c_int64)]


_libc = ctypes.CDLL(None, use_errno=True)
_libc.nanosleep.argtypes = [ctypes.POINTER(_Timespec), ctypes.POINTER(_Timespec)]
_libc.nanosleep.restype = ctypes.c_int


class SleepRetry(StepParticipant):
    def on_step(self, t, dt, inputs):
        req = _Timespec(0, BACKOFF_NS)
        iterations = 0
        err = 0
        while iterations < CAP:
            iterations += 1
            ctypes.set_errno(0)
            if _libc.nanosleep(ctypes.byref(req), None) != 0:
                err = ctypes.get_errno()
                break
        return [("readings", {"seq": iterations, "value": err})]


if __name__ == "__main__":
    run(SleepRetry())
