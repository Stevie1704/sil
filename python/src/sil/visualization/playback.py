"""Headless playback of a prepared Recording (spec #345 s3).

`Playback` is the tool-independent controller that external-tool adapters
drive. It reads a `Navigator` and keeps one playback state: a committed
cursor, a desired playing state, a rate and a Channel subscription. It
chooses no network protocol: an adapter calls `configure`, `status`,
`set_subscription`, `disconnect` and `close`, and forwards the
notifications that `drain` returns.

Time: an injected monotonic clock in integer nanoseconds paces playback.
The playhead advances by elapsed clock time times the rate from an anchor;
a rate change re-anchors at the current playhead, so the cursor does not
jump. Source times are never changed.

Threading: one thread calls `pump` (or `run`, which loops `pump`); only that
thread reads the Navigator. Every other call may come from any thread. All
state changes are serialized by one lock, which is not held while the
Navigator reads, so a command can supersede a reconstruction in progress.
Each seek, subscription change and disconnect starts a new generation; a
reconstruction or page read of an older generation is discarded when it
completes. At most one reconstruction is active, and the latest command
state is the one pending replacement.

Delivery: `drain` returns notifications in order. Continuous playback
enqueues every subscribed Message crossed, in (publication_time_ns,
record_index) order, as `Message`. A seek enqueues `Reset`, then the latest
Message per subscribed Channel at or before the seek time as `Backfill`,
then `Commit`. A Message the Navigator refuses as oversize is reported in
its place as `Refused`; nothing is skipped silently. The queue is bounded by
`QueueLimits`: when it is full, playback stops advancing and reports
`buffering` until the adapter drains it. A source that changes, or an index
that becomes unusable, ends the session with status `failed`.
"""

from __future__ import annotations

import math
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Collection
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from sil.visualization.navigation import (Description, Limits,
                                         MIB, LimitExceeded, Navigator,
                                         PageLimits, Progress,
                                         prepare_navigation, require_positive)
from sil.visualization.source import SourceError, SourceMessage

PAUSED = "paused"
PLAYING = "playing"
BUFFERING = "buffering"
ENDED = "ended"
FAILED = "failed"
CLOSED = "closed"
REQUEST_CACHE = 256
MIN_RATE = 0.1
MAX_RATE = 10.0
_AFTER_INSTANT = 2**63 - 1  # record_index after every Message of an instant
_MAX_PAGE_ROWS = 1000


@dataclass(frozen=True)
class QueueLimits:
    """Bounds of the notification queue: queued Messages (`Message`,
    `Backfill` and `Refused`) and their payload bytes. A Message larger
    than `max_bytes` is refused."""

    max_messages: int = 1024
    max_bytes: int = 16 * MIB

    def __post_init__(self):
        for name in ("max_messages", "max_bytes"):
            require_positive(name, getattr(self, name))


@dataclass(frozen=True)
class PlaybackState:
    """One consistent view of the controller.

    `status` is `paused`, `playing`, `buffering` (a reconstruction is
    pending, or delivery is slower than the rate), `ended`, `failed` or
    `closed`. `playing` is the desired state. `cursor_ns` is the committed
    cursor: every subscribed Message up to it is enqueued. `seek_ns` is the
    target of a pending seek. Bounds are inclusive; they and the cursor are
    None for a source without Messages."""

    session_id: str
    source_sha256: str
    generation: int
    status: str
    playing: bool
    pending: bool
    cursor_ns: int | None
    seek_ns: int | None
    start_ns: int | None
    end_ns: int | None
    rate: float
    request_id: str | None
    subscription: tuple[str, ...]
    diagnostic: str | None


@dataclass(frozen=True)
class Reset:
    """Discard all delivered history: a seek to `position_ns` follows."""

    generation: int
    position_ns: int | None


@dataclass(frozen=True)
class Backfill:
    """The latest Message of a Channel at or before the committed cursor,
    with its original time and locator. Not a new publication."""

    generation: int
    message: SourceMessage


@dataclass(frozen=True)
class Message:
    """A Message crossed by continuous playback."""

    generation: int
    message: SourceMessage


@dataclass(frozen=True)
class Refused:
    """A Message that a limit refused, in the place where it belongs. With
    `backfill` it stands for a Channel's latest value at a seek."""

    generation: int
    channel: str
    publication_time_ns: int
    record_index: int
    size_bytes: int
    backfill: bool
    reason: str


@dataclass(frozen=True)
class Commit:
    """A reconstruction completed; `state` holds the committed cursor."""

    state: PlaybackState


@dataclass(frozen=True)
class StateChanged:
    """The status changed without a command: buffering, ended or failed."""

    state: PlaybackState


Notification = Reset | Backfill | Message | Refused | Commit | StateChanged


class InvalidCommand(ValueError):
    """A command was refused. `state` is the actual, unchanged state."""

    def __init__(self, message: str, state: PlaybackState):
        super().__init__(message)
        self.state = state


class RequestConflict(InvalidCommand):
    """A cached request ID came with other content."""


@dataclass(frozen=True)
class _Job:
    generation: int
    reset: bool
    after: tuple[int, int] | None
    channels: tuple[str, ...]


@dataclass(frozen=True)
class _Read:
    generation: int
    channels: tuple[str, ...]
    after: tuple[int, int]
    target: int
    limits: PageLimits
    queue_empty: bool


def default_cache_dir() -> Path:
    """Where `open_playback` keeps navigation indexes without a
    `cache_dir`: `$XDG_CACHE_HOME/sil/navigation`, else
    `~/.cache/sil/navigation`. The indexes are disposable."""
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "sil" / "navigation"


def open_playback(source: str | Path, *, cache_dir: str | Path | None = None,
                  limits: Limits | None = None,
                  progress: Callable[[Progress], None] | None = None,
                  cancel: threading.Event | None = None,
                  clock: Callable[[], int] = time.monotonic_ns,
                  queue_limits: QueueLimits | None = None) -> Playback:
    """Prepare navigation of `source` (see `prepare_navigation`) and
    return a paused controller over it."""
    navigator = prepare_navigation(
        source, cache_dir=default_cache_dir() if cache_dir is None else cache_dir,
        limits=limits, progress=progress, cancel=cancel)
    return Playback(navigator, clock=clock, queue_limits=queue_limits)


class Playback:
    """The playback controller over one Navigator, which it owns and
    closes. It starts paused at the first instant of the source.
    `description` is the Navigator's `describe()` result: an adapter takes
    the Channel schemas for its payload conversion from it."""

    description: Description

    def __init__(self, navigator: Navigator, *,
                 clock: Callable[[], int] = time.monotonic_ns,
                 queue_limits: QueueLimits | None = None):
        described = navigator.describe()
        self.description = described  # source identity and Channel catalog
        self._nav = navigator
        self._clock = clock
        self._limits = queue_limits or QueueLimits()
        self._lock = threading.Condition()
        self._session = uuid.uuid4().hex
        self._sha256 = described.source.sha256
        self._channels = tuple(c.name for c in described.channels)
        self._start, self._end = described.start_ns, described.end_ns
        self._generation = 0
        self._playing = False
        self._rate = 1.0
        self._cursor = self._start
        self._after = None if self._start is None else (self._start, _AFTER_INSTANT)
        self._anchor = (0, self._cursor)
        self._stalled = False
        self._target = None
        self._ended = False
        self._closed = False
        self._busy = False
        self._more = False  # the last page read stopped before its target
        self._fault: str | None = None
        self._fresh = False  # after a disconnect: the next client needs a Reset
        self._subscription: tuple[str, ...] = ()
        self._unreconstructed: set[str] = set()
        self._reset_pending = False
        self._seek: int | None = None
        self._request_id: str | None = None
        self._requests: OrderedDict[str, tuple] = OrderedDict()
        self._queue: deque = deque()
        self._queued_messages = 0
        self._queued_bytes = 0

    # --- commands -----------------------------------------------------------

    def status(self) -> PlaybackState:
        with self._lock:
            return self._state()

    def configure(self, request_id: str | None = None, *,
                  position_ns: int | None = None, playing: bool | None = None,
                  rate: float | None = None) -> PlaybackState:
        """Seek, set the desired playing state and the rate in one atomic
        operation; None keeps a value. A seek returns a pending state; its
        `Commit` follows when the reconstruction completes. Raises
        InvalidCommand with the unchanged state.

        The last REQUEST_CACHE accepted request IDs are remembered: the same
        ID with the same content returns its first result without acting
        again; with other content it raises RequestConflict. An expired ID
        is a new request. `request_id=None` is never cached."""
        with self._lock:
            self._check_open()
            content = (position_ns, playing, rate)
            if request_id is not None and not isinstance(request_id, str):
                self._refuse(f"request_id must be a string, not {request_id!r}")
            if request_id is not None:
                cached = self._requests.get(request_id)
                if cached is not None:
                    if cached[0] != content:
                        raise RequestConflict(
                            f"request {request_id!r} was already used with "
                            "other content", self._state())
                    return cached[1]
            self._check_configure(position_ns, playing, rate)
            now = self._clock()
            if rate is not None and rate != self._rate:
                if self._running():
                    self._anchor = (now, self._playhead(now))
                self._rate = float(rate)
            if position_ns is not None:
                self._start_seek(position_ns)
            elif playing and self._ended and self._start is not None:
                self._start_seek(self._start)  # Play after the end restarts
            if playing is not None:
                self._set_playing(playing, now)
            self._request_id = request_id
            state = self._state()
            if request_id is not None:
                self._requests[request_id] = (content, state)
                if len(self._requests) > REQUEST_CACHE:
                    self._requests.popitem(last=False)
            self._lock.notify_all()
            return state

    def set_subscription(self, channels: Collection[str]) -> PlaybackState:
        """Deliver only `channels`. Newly subscribed Channels are
        reconstructed at the cursor before playback advances again; queued
        Messages of removed Channels are dropped."""
        with self._lock:
            self._check_open()
            names = list(channels)
            unknown = sorted(set(names) - set(self._channels))
            if unknown or len(set(names)) != len(names):
                raise InvalidCommand(
                    f"subscription names an unknown or repeated Channel: "
                    f"{unknown or names}", self._state())
            wanted = tuple(c for c in self._channels if c in set(names))
            if wanted == self._subscription:
                return self._state()
            removed = set(self._subscription) - set(wanted)
            added = set(wanted) - set(self._subscription)
            self._generation += 1
            self._unreconstructed = (self._unreconstructed & set(wanted)) | added
            self._subscription = wanted
            if self._stalled:  # time spent buffering is not played
                self._stalled, self._target = False, None
                self._anchor = (self._clock(), self._cursor)
            self._drop(lambda n: _channel(n) in removed)
            if self._fresh and wanted:
                self._fresh = False
                self._start_seek(self._seek if self._reset_pending
                                 else self._cursor)
            self._lock.notify_all()
            return self._state()

    def disconnect(self) -> PlaybackState:
        """The client went away: pause, drop its subscription, queued
        notifications and request IDs. The next subscription starts with a
        Reset and a fresh reconstruction at the cursor."""
        with self._lock:
            if self._closed:
                raise InvalidCommand("playback is closed", self._state())
            self._playing = False
            self._stalled, self._target = False, None
            self._subscription = ()
            self._unreconstructed = set()
            self._requests.clear()
            self._generation += 1
            self._drop(lambda n: True)
            self._fresh = True
            self._lock.notify_all()
            return self._state()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._playing = False
            self._drop(lambda n: True)
            self._enqueue(StateChanged(self._state()))
            if not self._busy:
                self._nav.close()
            self._lock.notify_all()

    def __enter__(self) -> Playback:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- delivery -----------------------------------------------------------

    def drain(self, max_items: int | None = None,
              timeout: float = 0.0) -> list[Notification]:
        """Take queued notifications in order. With `timeout`, wait up to
        that many seconds for the first one."""
        with self._lock:
            if not self._queue and timeout > 0:
                self._lock.wait_for(lambda: self._queue or self._closed,
                                    timeout)
            count = len(self._queue) if max_items is None else max_items
            taken = [self._queue.popleft()
                     for _ in range(min(count, len(self._queue)))]
            for notification in taken:
                self._account(notification, -1)
            if taken:
                self._lock.notify_all()
            return taken

    # --- work ---------------------------------------------------------------

    def pump(self) -> None:
        """Do the next unit of work: one reconstruction or one page of
        playback. Call it from one thread only."""
        with self._lock:
            if self._closed or self._busy or self._fault is not None:
                return
            work = self._take_job() or self._take_read()
            if work is None:
                return
            self._busy = True
        error = None
        try:
            if isinstance(work, _Job):
                result = self._reconstruct(work)
            else:
                result = self._read(work)
        except (SourceError, OSError) as e:  # source changed, index unusable
            error = e
        with self._lock:
            self._busy = False
            if self._closed:
                self._nav.close()
            elif error is not None:
                self._fail(error)
            elif isinstance(work, _Job):
                self._commit(work, result)
            else:
                self._advance(work, *result)
            self._lock.notify_all()

    def run(self, tick_ns: int = 10 * 1_000_000) -> None:
        """Pump until `close`, on the calling thread. While playing it
        pumps at least every `tick_ns` of wall time; otherwise it waits
        for a command or a drain."""
        while True:
            self.pump()
            with self._lock:
                if self._closed:
                    return
                if self._fault is None and (self._pending() or self._more):
                    continue
                self._lock.wait(tick_ns / 1e9 if self._playing else None)

    def _take_job(self) -> _Job | None:
        if not self._pending():
            return None
        if self._reset_pending:
            after = None if self._seek is None else (self._seek, _AFTER_INSTANT)
        else:
            after = self._after
        channels = tuple(c for c in self._subscription
                         if c in self._unreconstructed)
        return _Job(self._generation, self._reset_pending, after, channels)

    def _take_read(self) -> _Read | None:
        self._more = False
        if not self._playing or self._pending() or self._ended:
            return None
        target = self._target if self._stalled else self._playhead(self._clock())
        free_messages = self._limits.max_messages - self._queued_messages
        free_bytes = self._limits.max_bytes - self._queued_bytes
        if free_messages <= 0 or free_bytes <= 0:
            self._stall(target)
            return None
        if self._stalled and self._queued_messages:
            return None  # buffering lasts until the adapter drained the queue
        return _Read(self._generation, self._subscription, self._after, target,
                     PageLimits(min(free_messages, _MAX_PAGE_ROWS), free_bytes),
                     self._queued_messages == 0)

    def _reconstruct(self, job: _Job) -> list:
        if job.after is None:
            return []
        found = (self._predecessor(channel, job.after)
                 for channel in job.channels)
        return sorted((item for item in found if item is not None), key=_key)

    def _predecessor(self, channel: str, after: tuple[int, int]):
        """The last Message of `channel` at or before position `after`, a
        LimitExceeded naming it when it is refused, or None."""
        found = self._latest(channel, after[0])
        if found is None or _key(found) <= after:
            return found
        # The latest at this instant lies after the position (mid-Burst):
        # walk the instant up to the position, else take the instant before.
        best, position, complete = None, None, False
        while not complete:
            items, complete = self._page((channel,), after[0], after[0] + 1,
                                         position, PageLimits())
            for item in items:
                if _key(item) > after:
                    complete = True
                    break
                best = item
            position = _key(items[-1])
        if best is None and after[0] > 0:
            return self._latest(channel, after[0] - 1)
        return best

    def _latest(self, channel: str, time_ns: int):
        try:
            return self._nav.snapshot((channel,), time_ns)[channel]
        except LimitExceeded as e:
            if e.record_index is None:
                raise
            return e

    def _read(self, read: _Read) -> tuple[list, bool]:
        time_ns, record_index = read.after
        if not read.channels or time_ns > read.target or (
                time_ns == read.target and record_index == _AFTER_INSTANT):
            return [], True
        return self._page(read.channels, time_ns, read.target + 1, read.after,
                          read.limits)

    def _page(self, channels, start_ns, end_ns, after, limits) -> tuple[list, bool]:
        """One page after `after` (None: from `start_ns`). A refused
        Message comes back as its LimitExceeded, alone."""
        token = (None if after is None else
                 self._nav.continuation_after(channels, start_ns, end_ns, *after))
        try:
            page = self._nav.messages(channels, start_ns, end_ns, token, limits)
        except LimitExceeded as e:
            if e.record_index is None:
                raise
            return [e], False
        return list(page.messages), page.complete

    # --- state transitions (lock held) ----------------------------------------

    def _commit(self, job: _Job, items: list) -> None:
        if job.generation != self._generation:
            return  # superseded while it was read
        if job.reset:
            self._enqueue(Reset(job.generation, self._seek))
            self._cursor = self._seek
            self._after = job.after
            self._seek = None
            self._reset_pending = False
        for item in items:
            self._enqueue(_refused(item, job.generation, True)
                          if isinstance(item, LimitExceeded)
                          else Backfill(job.generation, item))
        self._unreconstructed.clear()
        if self._playing:
            self._anchor = (self._clock(), self._cursor)
        self._enqueue(Commit(self._state()))

    def _advance(self, read: _Read, items: list, complete: bool) -> None:
        if (read.generation != self._generation or not self._playing
                or self._pending() or self._ended):
            return  # paused or superseded while it was read
        if items and isinstance(items[0], LimitExceeded):
            if not read.queue_empty:
                self._stall(read.target)  # it may fit once the queue drains
                return
            self._enqueue(_refused(items[0], read.generation, False))
        else:
            for message in items:
                self._enqueue(Message(read.generation, message))
        if items:
            self._after = _key(items[-1])
            self._cursor = self._after[0]
        if not complete:
            self._more = True
            return
        self._after = (read.target, _AFTER_INSTANT)
        self._cursor = read.target
        if read.target == self._end:
            self._ended = True
            self._playing = False
            self._stalled = False
            self._enqueue(StateChanged(self._state()))
        elif self._stalled:
            self._stalled, self._target = False, None
            self._anchor = (self._clock(), self._cursor)
            self._enqueue(StateChanged(self._state()))

    def _stall(self, target: int) -> None:
        if not self._stalled:
            self._stalled, self._target = True, target
            self._enqueue(StateChanged(self._state()))

    def _start_seek(self, position_ns: int) -> None:
        self._generation += 1
        self._reset_pending = True
        self._seek = position_ns
        self._unreconstructed = set(self._subscription)
        self._ended = False
        self._stalled, self._target = False, None
        self._drop(lambda n: True)

    def _set_playing(self, playing: bool, now: int) -> None:
        if not playing:
            self._playing = False
            self._stalled, self._target = False, None
        elif self._start is None:
            self._ended = True  # no playback range: nothing to play
        elif not self._playing:
            self._playing = True
            self._anchor = (now, self._cursor)

    def _running(self) -> bool:
        return (self._playing and not self._pending() and not self._stalled
                and not self._ended)

    def _pending(self) -> bool:
        return self._reset_pending or bool(self._unreconstructed)

    def _playhead(self, now: int) -> int:
        anchor_now, anchor_ns = self._anchor
        elapsed = max(0, now - anchor_now)
        return min(self._end, anchor_ns + int(elapsed * Fraction(self._rate)))

    def _state(self) -> PlaybackState:
        if self._closed:
            status = CLOSED
        elif self._fault is not None:
            status = FAILED
        elif self._pending():
            status = BUFFERING
        elif self._ended:
            status = ENDED
        elif self._playing:
            status = BUFFERING if self._stalled else PLAYING
        else:
            status = PAUSED
        return PlaybackState(
            self._session, self._sha256, self._generation, status,
            self._playing, self._pending(), self._cursor, self._seek,
            self._start, self._end, self._rate, self._request_id,
            self._subscription, self._fault)

    def _check_open(self) -> None:
        if self._closed:
            raise InvalidCommand("playback is closed", self._state())
        if self._fault is not None:
            raise InvalidCommand(f"playback failed: {self._fault}", self._state())

    def _fail(self, error: Exception) -> None:
        self._fault = str(error)
        self._playing = False
        self._stalled, self._target = False, None
        self._enqueue(StateChanged(self._state()))

    def _check_configure(self, position_ns, playing, rate) -> None:
        if position_ns is not None:
            if isinstance(position_ns, bool) or not isinstance(position_ns, int):
                self._refuse(f"position_ns must be an integer, not {position_ns!r}")
            if self._start is None:
                self._refuse("the source has no Messages, so no playback range")
            if not self._start <= position_ns <= self._end:
                self._refuse(f"position_ns {position_ns} is outside "
                             f"{self._start}..{self._end}")
        if playing is not None and not isinstance(playing, bool):
            self._refuse(f"playing must be a bool, not {playing!r}")
        if rate is not None:
            if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                    or not math.isfinite(rate)
                    or not MIN_RATE <= rate <= MAX_RATE):
                self._refuse(f"rate must be a finite number in "
                             f"{MIN_RATE}..{MAX_RATE}, not {rate!r}")

    def _refuse(self, message: str):
        raise InvalidCommand(message, self._state())

    # --- queue (lock held) ------------------------------------------------------

    def _enqueue(self, notification: Notification) -> None:
        if (isinstance(notification, StateChanged) and self._queue
                and isinstance(self._queue[-1], StateChanged)):
            self._queue[-1] = notification  # only the latest state matters
            return
        self._queue.append(notification)
        self._account(notification, 1)

    def _drop(self, matches: Callable[[Notification], bool]) -> None:
        kept = deque(n for n in self._queue if not matches(n))
        self._queue = deque()
        self._queued_messages = self._queued_bytes = 0
        for notification in kept:
            self._queue.append(notification)
            self._account(notification, 1)

    def _account(self, notification: Notification, sign: int) -> None:
        if isinstance(notification, (Message, Backfill)):
            self._queued_messages += sign
            self._queued_bytes += sign * len(notification.message.data)
        elif isinstance(notification, Refused):
            self._queued_messages += sign


def _key(item) -> tuple[int, int]:
    """The (publication_time_ns, record_index) position of a Message or of a
    LimitExceeded that names one."""
    return item.publication_time_ns, item.record_index


def _refused(error: LimitExceeded, generation: int, backfill: bool) -> Refused:
    return Refused(generation, error.channel, error.publication_time_ns,
                   error.record_index, error.size_bytes, backfill, str(error))


def _channel(notification: Notification) -> str | None:
    if isinstance(notification, (Message, Backfill)):
        return notification.message.channel
    if isinstance(notification, Refused):
        return notification.channel
    return None
