"""Toy thread-starting process participant for the thread-policy suite (#262).

It starts one worker thread in on_init and one in each step, and joins each
worker before it continues, so a positive determinism check tests a defined
contract. CPython's threading module creates the thread through the
dynamically linked pthread_create, which the Clock shim interposes.

Published on toy.Counter: seq = 1 when this step's worker ran, value = 1 when
the init worker ran. A worker that cannot start counts as not run.
"""

from threading import Thread

from sil.participant import StepParticipant, run


def _run_worker() -> int:
    ran = []
    worker = Thread(target=ran.append, args=(1,))
    try:
        worker.start()
    except RuntimeError:  # pthread_create failed, e.g. with EAGAIN
        return 0
    worker.join()
    return len(ran)


class ThreadStart(StepParticipant):
    def on_init(self, init):
        self.init_ran = _run_worker()

    def on_step(self, t, dt, inputs):
        return [("readings", {"seq": _run_worker(), "value": self.init_ran})]


if __name__ == "__main__":
    run(ThreadStart())
