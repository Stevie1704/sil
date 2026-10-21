"""Loaded by batch GDB, not importable with ordinary Python.

Only reads inferior state. No injected calls, changed variables or source probes.
"""
import json
import os
from pathlib import Path
import time

import gdb

dest = Path(os.environ["SIL_DEBUG_DEST"])
selector = os.environ["SIL_DEBUG_SELECTOR"]
action = os.environ.get("SIL_DEBUG_ACTION", "resume")
stops = []
selection_stops = []


def evaluate(expression):
    try:
        value = gdb.parse_and_eval(expression)
        if value.type.code == gdb.TYPE_CODE_VOID:
            return {"value": None, "reason": "unset"}
        if value.is_optimized_out:
            return {"value": None, "reason": "optimized_out"}
        return {"value": str(value), "reason": None}
    except gdb.error as error:
        return {"value": None, "reason": str(error)}


def save():
    (dest / "stops.json").write_text(json.dumps(stops, indent=2) + "\n")


def inspect_selection(source, lines):
    """Inspect locals before last use; never fill missing fields from inputs."""
    anchor = "      if (hazard(&lead)) mode = ADAS_REF_MODE_HAZARD;"
    gdb.Breakpoint(f"{source}:{lines.index(anchor) + 1}", temporary=True)
    gdb.execute("continue")
    for instruction in range(32):
        newest = gdb.newest_frame()
        newest.select()
        inline = {name: evaluate(expr) for name, expr in
                  {"hazard_argument": "*radar", "closing_mps": "closing_mps"}.items()}
        frame = newest
        while frame and frame.name() != "adas_ref_advance":
            frame = frame.older()
        if frame is None:
            raise gdb.GdbError("selection inspection left adas_ref_advance")
        frame.select()
        values = {"lead": "lead", "selected": "selected", **{
            "lead." + field: "lead." + field
            for field in ("id", "x_m", "y_m", "relative_vx_mps", "confidence")}}
        ready = (all(evaluate(values[name])["value"] is not None
                     for name in ("selected", "lead.x_m", "lead.relative_vx_mps"))
                 and inline["closing_mps"]["value"] == "25")
        if instruction == 0 or ready:
            if not ready:
                inline["closing_mps"] = {"value": None, "reason": "before_hazard_computation"}
            snapshot("selection", values, inline_values=inline)
            selection_stops.append(stops.pop())
            (dest / "selection-stops.json").write_text(json.dumps(selection_stops, indent=2) + "\n")
            save()
        if ready:
            return
        newest.select()
        gdb.execute("nexti")
    raise gdb.GdbError("selected lead fields/hazard inputs unavailable within 32 instructions")


def snapshot(stage, expressions, *, inline_values=None):
    thread = gdb.selected_thread()
    frame = gdb.selected_frame()
    sal = frame.find_sal()
    row = {
        "stage": stage, "hit": entry.index, "virtual_time_ns": entry.t,
        "pid": gdb.selected_inferior().pid,
        "pgid": os.getpgid(gdb.selected_inferior().pid),
        "ptid": list(thread.ptid), "host_monotonic_ns": time.monotonic_ns(),
        "source": sal.symtab.fullname() if sal.symtab else None,
        "line": sal.line,
        "values": {name: evaluate(expr) for name, expr in expressions.items()},
        "stack": gdb.execute("bt 16", to_string=True),
        "threads": gdb.execute("info threads", to_string=True),
        "locals": gdb.execute("info locals", to_string=True),
    }
    if inline_values is not None:
        row["inline_values"] = inline_values
    if stage == "entry":
        row["modules"] = gdb.execute("info sharedlibrary", to_string=True)
        row["proc_status"] = Path(f"/proc/{row['pid']}/status").read_text()
        row["parent_pid"] = int(next(x.split()[1] for x in
            row["proc_status"].splitlines() if x.startswith("PPid:")))
        # Read the shim's actual mapped state; do not inject clock calls.
        if os.environ.get("SIL_DEBUG_SHIM"):
            row["shim_virtual_time"] = evaluate("g_region->t")
            row["shim_clock_symbol"] = gdb.execute("info address clock_gettime", to_string=True)
    stops.append(row)
    save()


class Entry(gdb.Breakpoint):
    def __init__(self):
        super().__init__("adas_ref_advance")
        self.index = -1
        self.primary = None

    def stop(self):
        address = int(gdb.parse_and_eval("instance"))
        if self.primary is None:
            self.primary = address
        if address != self.primary:
            return False  # Another FMU in the same process is not another Step.
        self.index += 1
        self.t = int(gdb.parse_and_eval("t_ns"))
        return (self.index in (4, 5) if selector == "index"
                else self.t in (40_000_000, 50_000_000))


gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set breakpoint pending on")
gdb.execute("set print elements 32")
gdb.execute("set debuginfod enabled off")
# The pinned GDB's --args quotes whitespace/JSON for its startup shell.
# Switching that shell off after argument parsing loses the adapter argv.
gdb.execute("set startup-with-shell on")
gdb.execute("set disable-randomization off")
gdb.execute("handle SIGTERM nostop print pass")
gdb.execute("handle SIGPIPE nostop print pass")
if os.environ.get("SIL_DEBUG_SHIM"):
    gdb.execute("set environment LD_PRELOAD " + os.environ["SIL_DEBUG_SHIM"])
entry = Entry()
gdb.execute("run")
while gdb.selected_inferior().pid:
    if gdb.selected_frame().name() != "adas_ref_advance":
        raise gdb.GdbError("unexpected stop; retain raw debugger log")
    instance = int(gdb.parse_and_eval("instance"))
    output = int(gdb.parse_and_eval("output"))
    snapshot("entry", {
        "instance": "*instance", "radar": "instance->radar",
        "camera": "instance->camera", "ego": "instance->ego_held",
        "previous_command": "instance->acceleration_mps2",
        "activation_count": "instance->activations", "t_ns": "t_ns",
    })
    if action != "resume":
        (dest / "action.json").write_text(json.dumps({"action": action,
            "inferior_pid": gdb.selected_inferior().pid, "index": entry.index,
            "virtual_time_ns": entry.t}) + "\n")
    if action == "detach":
        pid = gdb.selected_inferior().pid
        gdb.execute("detach")
        # Detach releases ptrace, not GDB's responsibility to reap its child.
        # Keep the wrapper alive until the resumed inferior has exited.
        _, status = os.waitpid(pid, 0)
        if os.waitstatus_to_exitcode(status) != 0:
            raise gdb.GdbError(f"detached inferior failed: wait status {status}")
        break
    if action == "kill":
        gdb.execute("kill")
        break
    if action == "debugger-error":
        raise gdb.GdbError("deliberate debugger failure")
    if action == "signal":
        entry.enabled = False
        gdb.execute("signal SIGTERM")
        break
    if action == "deadline":
        time.sleep(3)
        gdb.execute("continue")
        break
    # Source anchors deliberately fail when this named workload changes.
    source = Path(os.environ["SIL_DEBUG_SOURCE"])
    lines = source.read_text().splitlines()
    line = next(i + 1 for i, text in enumerate(lines)
                if text == "  instance->acceleration_mps2 = limited(")
    entry.enabled = False
    inspect_selection(source, lines)
    decision = gdb.Breakpoint(f"{source}:{line}", temporary=True)
    gdb.execute("continue")
    inline_values = {name: evaluate(expr) for name, expr in
                     {"limited_from": "from", "limited_to": "to",
                      "limited_max_change": "max_change"}.items()}
    frame = gdb.selected_frame()
    while frame and frame.name() != "adas_ref_advance":
        frame = frame.older()
    if frame is None:
        raise gdb.GdbError("decision breakpoint is outside adas_ref_advance")
    frame.select()
    snapshot("decision", {"lead": "lead", "selected": "selected", "mode": "mode",
        "target": "target", "previous_command": "instance->acceleration_mps2"},
        inline_values=inline_values)
    # 'finish' stops after the C API returns, before adapter publication.
    gdb.execute("finish")
    snapshot("return", {
        "output": f"*(adas_ref_output *){output}",
        "limited_result": f"((adas_ref_instance *){instance})->acceleration_mps2",
        "sequence": f"((adas_ref_output *){output})->sequence",
        "sample_time_ns": f"((adas_ref_output *){output})->sample_time_ns",
        "selected": f"((adas_ref_output *){output})->selected_object_id",
        "mode": f"((adas_ref_output *){output})->mode",
        "target": f"((adas_ref_output *){output})->target_acceleration_mps2",
    })
    entry.enabled = True
    gdb.execute("continue")

(dest / "debugger-outcome.json").write_text(json.dumps({
    "exit_code": evaluate("$_exitcode"), "exit_signal": evaluate("$_exitsignal"),
    "action": action}) + "\n")
