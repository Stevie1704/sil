"""Bind descendant membership to observed fork/exit lifetimes, not numeric TIDs."""


def runner_fork_index(events, runner_pid, observations):
    anchors = [int(row["begin_ns"]) for row in observations if "begin_ns" in row]
    if not anchors or runner_pid is None:
        return None
    anchor = min(anchors)
    candidate = None
    for index, event in enumerate(events):
        if event["time_ns"] > anchor:
            break
        if event["event"] == "fork" and event["child_pid"] == runner_pid:
            candidate = index
        elif event["event"] == "exit" and event["pid"] == runner_pid:
            candidate = None
    return candidate


def scope_lifetimes(events, runner_pid, observations):
    root_index = runner_fork_index(events, runner_pid, observations)
    active, scoped = {}, []
    for index, original in enumerate(events):
        event = dict(original)
        at, kind = event["time_ns"], event["event"]
        if kind == "fork":
            child = event["child_pid"]
            close_lifetime(active, child, at)
            event["in_scope"] = index == root_index or event["parent_pid"] in active
            if event["in_scope"]:
                event["lifetime_end_ns"] = None
                active[child] = event
        elif kind == "exit":
            close_lifetime(active, event["pid"], at)
        elif kind == "exec" and event["old_pid"] != event["pid"]:
            close_lifetime(active, event["old_pid"], at)
            close_lifetime(active, event["pid"], at)
        scoped.append(event)
    return scoped


def close_lifetime(active, tid, at):
    if tid in active:
        active.pop(tid)["lifetime_end_ns"] = at


def spawn_lifetime_verified(events, marker, runner_pid, spawn_ns):
    # A spawn observation owns its first matching birth, never a later reuse.
    birth = next((event for event in events if event["event"] == "fork"
                  and event["child_pid"] == marker["tid"] and event["parent_pid"] == runner_pid
                  and spawn_ns <= event["time_ns"]), None)
    return (birth is not None and birth.get("in_scope")
            and birth["time_ns"] <= marker["host_ns"]
            and (birth["lifetime_end_ns"] is None or marker["host_ns"] < birth["lifetime_end_ns"]))
