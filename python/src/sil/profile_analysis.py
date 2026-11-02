"""Rank normalized profile evidence, including rows outside summary top lists.

Function identities use module paths and symbols, never artifact-local m1/m2
IDs. Inclusive weights count each function once per sample or allocation info.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Iterator

from sil.profile_set import ProfileSet

Function = tuple[str | None, str | None]
UNKNOWN: Function = (None, None)


def profile_tasks(profile: ProfileSet) -> list[tuple[str, str]]:
    """Stable Task positions shared by filters, timeline links and details."""
    assert profile.correlation is not None
    return sorted({(a["participant"], a["task"])
                   for a in profile.correlation["activations"]})


def function_paths(profile: ProfileSet) -> tuple[list[Function], list[list[int]]]:
    capture = profile.report["capture"]
    document = profile.samples if capture["kind"] == "cpu" else profile.allocations
    assert document is not None, "normalized evidence is required"
    modules = capture if capture["kind"] == "cpu" else capture["analysis"]
    paths = {m["id"]: m["path"] for m in modules["modules"]}
    keys: dict[Function, int] = {}
    frames = [keys.setdefault((paths.get(f["module"]), f["symbol"]), len(keys))
              for f in document["frames"]]
    # Empty stacks are evidence too, with an unknown leaf.
    unknown = keys.setdefault(UNKNOWN, len(keys))
    stacks = [[frames[f] for f in reversed(stack)] or [unknown]
              for stack in document["stacks"]]
    return list(keys), stacks


@dataclass
class CpuRanking:
    samples: int
    weight: int
    self: dict[int, tuple[int, int]]
    inclusive: dict[int, tuple[int, int]]


def cpu_ranking(paths: list[list[int]], rows: Iterable[list]) -> CpuRanking:
    """Rows are [path, thread, class, ref, sample count, weight ns]."""
    own = defaultdict(lambda: [0, 0])
    inclusive = defaultdict(lambda: [0, 0])
    samples = weight = 0
    for path, _thread, _cls, _ref, count, value in rows:
        samples += count
        weight += value
        own[paths[path][-1]][0] += count
        own[paths[path][-1]][1] += value
        for key in set(paths[path]):
            inclusive[key][0] += count
            inclusive[key][1] += value
    return CpuRanking(samples, weight, {k: (v[0], v[1]) for k, v in own.items()},
                      {k: (v[0], v[1]) for k, v in inclusive.items()})


def iter_cpu_rows(profile: ProfileSet) -> Iterator[list]:
    """Selected samples as path/thread/class/reference/count/weight rows.

    Static analyses and bounded interactive aggregation share this predicate;
    iteration does not allocate another full-size sample table.
    """
    assert profile.samples is not None, "normalized samples are required"
    classes = profile.correlation["samples"] if profile.correlation else None
    selected = set(profile.selection) if profile.selection is not None else None
    for index, sample in enumerate(profile.samples["samples"]):
        cls, ref = classes[index] if classes else ("", "")
        if selected is not None and (cls not in ("execution_context", "window_associated")
                                     or ref not in selected):
            continue
        yield [sample[4], sample[2], cls, ref, 1, sample[3]]


def cpu_rows(profile: ProfileSet) -> list[list]:
    return list(iter_cpu_rows(profile))
