"""Disposable analysis of the perf and Heaptrack proof evidence (issue #314).

It reads backend output only: `perf script` text, the Heaptrack data file
and the workload's sidecar intervals. It is not the production
normalization; #315 and #317 define that contract. The functions here are
pure, so tests/test_profiling_proof.py pins them on hand-written fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- perf script ------------------------------------------------------------

# `perf script -F comm,pid,tid,time,ip,sym,dso --ns` with call chains: one
# header line per sample, one indented line per frame, a blank line between
# samples. comm can contain spaces, so the header is matched from the right.
_HEADER = re.compile(
    r"^(?P<comm>\S.*?)\s+(?P<pid>-?\d+)/(?P<tid>-?\d+)\s+"
    r"(?P<sec>\d+)\.(?P<nsec>\d{9}):(?P<rest>.*)$")
_FRAME = re.compile(
    r"^\s+(?P<ip>[0-9a-f]+)\s+(?P<sym>.*?)\s+\((?P<dso>[^()]*)\)\s*$")
_OFFSET = re.compile(r"\+0x[0-9a-f]+$")


@dataclass(frozen=True)
class Frame:
    ip: int
    symbol: str | None  # None: perf printed [unknown]
    dso: str


@dataclass(frozen=True)
class Sample:
    comm: str
    pid: int
    tid: int
    time_ns: int
    frames: tuple[Frame, ...]

    @property
    def leaf(self) -> Frame | None:
        return self.frames[0] if self.frames else None


def _frame(match: re.Match) -> Frame:
    symbol = _OFFSET.sub("", match["sym"])
    return Frame(int(match["ip"], 16),
                 None if symbol == "[unknown]" else symbol, match["dso"])


def parse_perf_script(text: str) -> list[Sample]:
    """Every sample of a `perf script` call-chain listing, in input order.

    The time is kept as integer nanoseconds from the printed digits, never
    through a float."""
    samples: list[Sample] = []
    header: re.Match | None = None
    frames: list[Frame] = []

    def close() -> None:
        if header is not None:
            samples.append(Sample(
                header["comm"], int(header["pid"]), int(header["tid"]),
                int(header["sec"]) * 1_000_000_000 + int(header["nsec"]),
                tuple(frames)))

    for line in text.splitlines():
        if not line.strip():
            continue
        if (match := _HEADER.match(line)) is not None:
            close()
            header, frames = match, []
            if (inline := _FRAME.match(match["rest"])) is not None:
                frames.append(_frame(inline))
        elif header is not None and (match := _FRAME.match(line)):
            frames.append(_frame(match))
    close()
    return samples


# --- sidecar intervals and the sample join ----------------------------------


@dataclass(frozen=True)
class Interval:
    """One hot call, as the workload wrote it: host CLOCK_MONOTONIC ns."""

    role: str
    index: int
    function: str
    begin_ns: int
    end_ns: int
    pid: int
    tid: int


def parse_sidecar(text: str) -> list[Interval]:
    intervals = []
    for line in text.splitlines():
        if not line.strip():
            continue
        role, index, function, begin, end, pid, tid = line.split("\t")
        intervals.append(Interval(role, int(index), function, int(begin),
                                  int(end), int(pid), int(tid)))
    return intervals


@dataclass(frozen=True)
class JoinCount:
    inside: int = 0
    outside: int = 0
    inside_other_function: int = 0


def _contains(interval: Interval, sample: Sample) -> bool:
    return (interval.pid == sample.pid
            and interval.begin_ns <= sample.time_ns < interval.end_ns)


def join_samples(samples: list[Sample],
                 intervals: list[Interval]) -> dict[str, JoinCount]:
    """Per hot function: its leaf samples inside its own half-open intervals
    of the same process, outside them, and inside an interval of another
    hot function of that process. Only a sample's own PID joins: a time
    overlap in another process is never a match."""
    functions = {i.function for i in intervals}
    counts = {f: [0, 0, 0] for f in functions}
    for sample in samples:
        leaf = sample.leaf
        if leaf is None or leaf.symbol not in functions:
            continue
        count = counts[leaf.symbol]
        own = any(i.function == leaf.symbol and _contains(i, sample)
                  for i in intervals)
        count[0 if own else 1] += 1
        if any(i.function != leaf.symbol and _contains(i, sample)
               for i in intervals):
            count[2] += 1
    return {f: JoinCount(*c) for f, c in sorted(counts.items())}


# --- Heaptrack data file ----------------------------------------------------
#
# The interpreted format of Heaptrack 1.4 and 1.5 (src/util/linewriter.h,
# src/interpret/heaptrack_interpret.cpp, src/analyze/accumulatedtracedata.cpp):
# one record per line, a mode character first, numbers in hexadecimal.
#   v <heaptrack version> <file version>   file version 3: sized strings
#   X <command line>                       the traced process
#   s [<length>] <string>                  string i is the i-th s line
#   i <ip> <module string> [<function string> [<file string> <line>
#     [<inlined function> <inlined file> <inlined line>]...]]
#   t <ip index> <parent trace index>      trace i is the i-th t line; 0: root
#   a <size> <trace index>                 allocation info, 0-based
#   + <allocation info index>  /  - <allocation info index>
#   c <elapsed ms since the tracker started>
#   R <RSS pages>


@dataclass(frozen=True)
class SiteCount:
    calls: int = 0
    bytes: int = 0
    outstanding_calls: int = 0
    outstanding_bytes: int = 0


@dataclass
class HeapCapture:
    file_version: int = 0
    exe: str | None = None
    timestamps_ms: list[int] = field(default_factory=list)
    rss_samples: int = 0
    strings: list[str] = field(default_factory=list)
    # Per IP index - 1: the function strings of the frame and its inlined
    # frames (0 = unknown).
    ip_functions: list[tuple[int, ...]] = field(default_factory=list)
    ip_modules: list[int] = field(default_factory=list)
    traces: list[tuple[int, int]] = field(default_factory=list)
    infos: list[tuple[int, int]] = field(default_factory=list)
    allocated: list[int] = field(default_factory=list)
    freed: list[int] = field(default_factory=list)

    def _string(self, index: int) -> str | None:
        return self.strings[index - 1] if 0 < index <= len(self.strings) \
            else None

    def trace_functions(self, trace: int) -> list[str | None]:
        """The function names of a trace, innermost first."""
        names: list[str | None] = []
        while trace:
            ip, trace = self.traces[trace - 1]
            names.extend(self._string(f) for f in self.ip_functions[ip - 1])
        return names

    def modules(self) -> set[str]:
        return {m for i in self.ip_modules if (m := self._string(i))}

    def _infos_through(self, function: str) -> set[int]:
        return {index for index, (_, trace) in enumerate(self.infos)
                if function in self.trace_functions(trace)}

    def allocations_through(self, function: str) -> SiteCount:
        """Allocations whose stack contains `function`: an inclusive cost.
        Outstanding means not freed when the capture ended; it is not a
        leak verdict."""
        selected = self._infos_through(function)
        calls = bytes_ = out_calls = out_bytes = 0
        for index in selected:
            size = self.infos[index][0]
            live = self.allocated[index] - self.freed[index]
            calls += self.allocated[index]
            bytes_ += self.allocated[index] * size
            out_calls += live
            out_bytes += live * size
        return SiteCount(calls, bytes_, out_calls, out_bytes)

    def sizes_through(self, function: str) -> set[int]:
        return {self.infos[i][0] for i in self._infos_through(function)
                if self.allocated[i]}


def _hex_fields(rest: str) -> list[int]:
    return [int(token, 16) for token in rest.split()]


def parse_heaptrack(text: str) -> HeapCapture:
    capture = HeapCapture()
    for line in text.splitlines():
        if len(line) < 1:
            continue
        mode, rest = line[0], line[2:]
        if mode == "v":
            capture.file_version = _hex_fields(rest)[1]
        elif mode == "X":
            capture.exe = rest
        elif mode == "s":
            if capture.file_version >= 3:
                length, _, string = rest.partition(" ")
                capture.strings.append(string[:int(length, 16)])
            else:
                capture.strings.append(rest)
        elif mode == "i":
            values = _hex_fields(rest)
            capture.ip_modules.append(values[1])
            frames = values[2:]
            functions = (frames[0],) if frames else (0,)
            functions += tuple(frames[3::3]) if len(frames) > 3 else ()
            capture.ip_functions.append(functions)
        elif mode == "t":
            ip, parent = _hex_fields(rest)
            capture.traces.append((ip, parent))
        elif mode == "a":
            size, trace = _hex_fields(rest)
            capture.infos.append((size, trace))
            capture.allocated.append(0)
            capture.freed.append(0)
        elif mode == "+":
            capture.allocated[_hex_fields(rest)[0]] += 1
        elif mode == "-":
            capture.freed[_hex_fields(rest)[0]] += 1
        elif mode == "c":
            capture.timestamps_ms.append(_hex_fields(rest)[0])
        elif mode == "R":
            capture.rss_samples += 1
    return capture


# --- module identity --------------------------------------------------------

_BUILD_ID = re.compile(r"Build ID:\s*([0-9a-f]+)")


def readelf_build_id(notes: str) -> str | None:
    """The GNU build ID in `readelf -n` output, or None."""
    match = _BUILD_ID.search(notes)
    return match[1] if match else None


def parse_buildid_list(listing: str) -> dict[str, str]:
    """`perf buildid-list` output: module path to build ID."""
    modules = {}
    for line in listing.splitlines():
        build_id, _, path = line.strip().partition(" ")
        if path:
            modules[path] = build_id
    return modules


# --- capability probes ------------------------------------------------------

_PERMISSION = ("no permission", "perf_event_paranoid", "access to performance",
               "operation not permitted", "permission denied")
_MISSING = ("not found for kernel", "command not found")


def probe_outcome(returncode: int | None, stderr: str, *,
                  produced: bool) -> str:
    """The outcome of one capability probe. returncode None: the tool could
    not be started."""
    text = stderr.lower()
    if returncode is None or any(m in text for m in _MISSING):
        return "missing"
    if returncode == 0:
        return "available" if produced else "failed"
    if any(p in text for p in _PERMISSION):
        return "permission-denied"
    if "not supported" in text:
        return "unsupported"
    return "failed"
