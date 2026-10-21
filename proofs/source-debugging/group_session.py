"""Batch GDB: stop each of three real importer-group instances in one Step."""
import json
import os
from pathlib import Path
import time

import gdb

dest = Path(os.environ["SIL_DEBUG_DEST"])
stops = []


class GroupStep(gdb.Breakpoint):
    def __init__(self):
        super().__init__("fmi3DoStep")
        self.primary = None
        self.index = -1
        self.selected = False
        self.seen = set()

    def stop(self):
        instance = int(gdb.parse_and_eval("instance"))
        if self.primary is None:
            self.primary = instance
        if instance == self.primary:
            self.index += 1
            t = round(float(gdb.parse_and_eval("communication_point")) * 1e9)
            self.selected = (self.index == 2 if os.environ["SIL_DEBUG_SELECTOR"] == "index"
                             else t == 200_000_000)
        if not self.selected or instance in self.seen:
            return False
        self.seen.add(instance)
        return True


gdb.execute("set confirm off")
gdb.execute("set breakpoint pending on")
gdb.execute("set print elements 32")
gdb.execute("set debuginfod enabled off")
gdb.execute("set disable-randomization off")
entry = GroupStep()
gdb.execute("run")
while gdb.selected_inferior().pid:
    inferior = gdb.selected_inferior()
    frame = gdb.selected_frame()
    instance = int(gdb.parse_and_eval("instance"))
    source = frame.find_sal()
    state_type = "Bus" if source.symtab.fullname().endswith("fmi_bus.c") else "Node"
    expression = f"*({state_type} *){instance}"
    state = str(gdb.parse_and_eval(expression))
    time.sleep(0.05)
    status = Path(f"/proc/{inferior.pid}/status").read_text()
    stops.append({"stage": "entry", "hit": entry.index,
        "virtual_time_ns": round(float(gdb.parse_and_eval("communication_point")) * 1e9),
        "instance": hex(instance), "pid": inferior.pid, "pgid": os.getpgid(inferior.pid),
        "parent_pid": int(next(x.split()[1] for x in status.splitlines() if x.startswith("PPid:"))),
        "host_monotonic_ns": time.monotonic_ns(), "state": state,
        "state_unchanged_while_stopped": state == str(gdb.parse_and_eval(expression)),
        "all_threads_stopped": all(t.is_stopped() for t in inferior.threads()),
        "children": Path(f"/proc/{inferior.pid}/task/{inferior.pid}/children").read_text(),
        "module": frame.function().symtab.objfile.filename,
        "modules": gdb.execute("info sharedlibrary", to_string=True),
        "maps": Path(f"/proc/{inferior.pid}/maps").read_text(),
        "stack": gdb.execute("bt 16", to_string=True),
        "source": source.symtab.fullname(), "line": source.line})
    (dest / "stops.json").write_text(json.dumps(stops, indent=2) + "\n")
    if len(stops) == 3:
        entry.enabled = False
    gdb.execute("continue")
