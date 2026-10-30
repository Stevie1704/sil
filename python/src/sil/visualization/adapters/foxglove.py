"""Foxglove adapter: the Foxglove playback bar drives the headless
playback controller (spec #345 s3, issue #349).

`serve(playback, host, port)` starts a Foxglove SDK WebSocket server with
the `Time` and `PlaybackControl` capabilities and the source's inclusive
time range, and a relay (`foxglove_relay`) in front of it on `host:port`.
Clients connect to the relay. Each source Channel is a topic with the
export's JSON Schema and payloads (`interchange.ChannelCodec`); every
Message has its publication time as log time.

Mapping:

- A playback control request is one `Playback.configure`: Play/Pause sets
  the desired playing state, the speed sets the rate, a seek time seeks.
  The answer is the actual state; a refused request answers the unchanged
  state and sends a warning status with the diagnostic.
- Subscribe/unsubscribe set the controller's subscription to the union of
  the client's topics.
- Only the relay's client controls playback. The SDK server listens on a
  loopback port that other local processes can reach; the relay marks its
  client's requests with one-time tokens (see `foxglove_relay`), and the
  bridge ignores every other request. Such a client gets an error status
  for its request, and copies of the data, but no control.
- `Reset` (a seek, or a new client): first all Messages logged before it
  leave the SDK queue, then a playback state with `did_seek` reaches the
  client, then the Backfill follows. So no value from before the seek
  arrives after the client cleared its history.
- `Commit` and `StateChanged` are broadcast as playback states; the cursor
  is broadcast as server time.
- `Refused` and payloads above `max_message_bytes` are sent as warning
  statuses, never as data.
- A disconnect pauses the controller, and a new session ID makes the next
  client start fresh.

Flow control: fewer Messages than the SDK's per-client backlog are ever
unacknowledged by the relay, so the SDK's lossy queue never drops one. A
slow client stops acknowledgements; the adapter then stops draining the
controller, which reports buffering.

The import of `foxglove` is here only: export and the other commands work
without the `sil[foxglove]` extra.
"""

from __future__ import annotations

import threading
import time

from sil.visualization import interchange
from sil.visualization.adapters import AdapterUnavailable
from sil.visualization.adapters.foxglove_relay import (BARRIER_ID, Relay,
                                                       Subscription)
from sil.visualization.interchange import ChannelCodec
from sil.visualization.playback import (Backfill, Commit, InvalidCommand,
                                        Message, Playback, PlaybackState,
                                        Refused, Reset, StateChanged)

try:
    import foxglove
    from foxglove.websocket import (Capability, PlaybackCommand,
                                    PlaybackControlRequest, ServerListener,
                                    StatusLevel)
    from foxglove.websocket import PlaybackState as FoxgloveState
    from foxglove.websocket import PlaybackStatus as FoxgloveStatus
except ImportError as e:
    raise AdapterUnavailable("the Foxglove adapter needs the foxglove-sdk "
                             "package") from e

S = 1_000_000_000
# serverInfo carries the range as {sec: u32, nsec}: later times cannot be shown.
MAX_TIME_NS = (2**32 - 1) * S + S - 1
MIB = 1 << 20
_RETRY_S = 0.5  # re-send an unacknowledged barrier or seek state
_POLL_S = 0.05
# Foxglove has no failed or closed status. Both stop playback, so they show
# as Paused; a failure also sends an error status with its diagnostic.
_STATUS = {"playing": FoxgloveStatus.Playing, "paused": FoxgloveStatus.Paused,
           "buffering": FoxgloveStatus.Buffering, "ended": FoxgloveStatus.Ended,
           "failed": FoxgloveStatus.Paused, "closed": FoxgloveStatus.Paused}


class UnsupportedSource(ValueError):
    """The source cannot be served: no Messages, or times out of range."""


def serve(playback: Playback, host: str, port: int, *, backlog: int = 1024,
          max_message_bytes: int = 8 * MIB,
          stall_timeout: float = 30.0) -> Bridge:
    """Serve `playback` to one Foxglove client at a time on `host:port`
    (0 picks a free port). The bridge owns `playback` and closes it."""
    return Bridge(playback, host, port, backlog=backlog,
                  max_message_bytes=max_message_bytes,
                  stall_timeout=stall_timeout)


class Bridge:
    """One served playback: the SDK server, its relay and two threads, one
    pumping the controller and one delivering its notifications."""

    def __init__(self, playback: Playback, host: str, port: int, *,
                 backlog: int, max_message_bytes: int, stall_timeout: float):
        described = playback.description
        if described.start_ns is None:
            raise UnsupportedSource("the source has no Messages, so it has no "
                                    "playback range; export it instead")
        if described.end_ns > MAX_TIME_NS:
            raise UnsupportedSource(
                f"the source ends at {described.end_ns} ns; Foxglove playback "
                f"ranges end at {MAX_TIME_NS} ns (2^32 s)")
        if backlog < 4:
            raise ValueError("backlog must be at least 4")
        self._playback = playback
        self._window = backlog // 2  # unacknowledged data-queue entries
        self._max_message_bytes = max_message_bytes
        self._closed = False
        self._sessions = 0
        self._changed = threading.Condition()
        self._next_barrier = 0
        self._latest_logged: int | None = None
        self._reset_accounting()
        self._codecs = {c.name: ChannelCodec(c.schema_name, c.schema)
                        for c in described.channels}
        self._context = foxglove.Context()
        self._channels = {c.name: self._channel(c, described.source.sha256)
                          for c in described.channels}
        self._topics = {channel.id(): name
                        for name, channel in self._channels.items()}
        self._relay: Relay | None = None
        self._server = foxglove.start_server(
            name="sil playback", host="127.0.0.1", port=0,
            capabilities=[Capability.Time, Capability.PlaybackControl],
            playback_time_range=(described.start_ns, described.end_ns),
            server_listener=_Listener(self), message_backlog_size=backlog,
            context=self._context, session_id=self._session_id())
        try:
            self._relay = Relay(host, port, self._server.port,
                                on_connect=self._connected,
                                on_disconnect=self._disconnected,
                                changed=self._changed,
                                stall_timeout=stall_timeout)
        except OSError:
            self._server.stop()
            raise
        self.url = f"ws://{_url_host(host)}:{self._relay.port}"
        self._threads = [
            threading.Thread(target=playback.run, daemon=True,
                             name="sil-playback-pump"),
            threading.Thread(target=self._deliver, daemon=True,
                             name="sil-foxglove-delivery"),
        ]
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        with self._changed:
            if self._closed:
                return
            self._closed = True
            self._changed.notify_all()
        self._playback.close()
        self._relay.close()
        for thread in self._threads:
            thread.join(timeout=5)
        self._server.stop()

    # --- SDK callbacks (SDK threads) -----------------------------------------

    def control(self, request: PlaybackControlRequest) -> FoxgloveState | None:
        """Act on a request of the relay's client. None for a request of
        any other client: the SDK answers that client with an error."""
        relay = self._relay  # None while the bridge starts
        claimed = relay.claim(request.request_id) if relay else None
        if isinstance(claimed, Subscription):
            self._subscribe(claimed.channel_ids)
            # The relay drops this answer to its fence.
            return _foxglove_state(self._playback.status(), did_seek=False,
                                   request_id=request.request_id)
        if claimed is None:
            return None
        try:
            state = self._playback.configure(
                claimed.request_id or None,
                position_ns=request.seek_time,
                playing=request.playback_command == PlaybackCommand.Play,
                rate=float(f"{request.playback_speed:.6g}"))
        except InvalidCommand as e:
            self._server.publish_status(f"SiL refused the playback request: {e}",
                                        StatusLevel.Warning)
            state = e.state
        # did_seek is sent by the delivery thread, ordered with the data.
        return _foxglove_state(state, did_seek=False,
                               request_id=claimed.request_id)

    def _subscribe(self, channel_ids: frozenset[int]) -> None:
        topics = sorted(self._topics[c] for c in channel_ids if c in self._topics)
        try:
            self._playback.set_subscription(topics)
        except InvalidCommand:
            pass  # closed or failed: the state change was already reported

    # --- relay callbacks (relay threads) -------------------------------------

    def _connected(self) -> None:
        self._reset_accounting()

    def _disconnected(self) -> None:
        with self._changed:
            if self._closed:
                return
        try:
            self._playback.disconnect()
        except InvalidCommand:
            return
        self._reset_accounting()
        # A reconnecting client sees a new session and drops what it holds.
        self._server.clear_session(self._session_id())

    # --- delivery (one thread) -----------------------------------------------

    def _deliver(self) -> None:
        while not self._closed:
            room = self._room()
            if room <= 0:
                self._await_acknowledgement()
                continue
            self._latest_logged = None
            notifications = self._playback.drain(max_items=room, timeout=_POLL_S)
            for notification in notifications:
                if self._closed:
                    return
                self._handle(notification)
            self._advance_time(self._latest_logged)
            if self._unacknowledged():
                self._barrier()
            elif not notifications:  # idle and delivered: show the cursor
                self._advance_time(self._playback.status().cursor_ns)

    def _advance_time(self, time_ns: int | None, *, jump: bool = False) -> None:
        """Broadcast the server time: forward only, except at a seek."""
        if time_ns is None or (not jump and self._time is not None
                               and time_ns <= self._time):
            return
        self._time = time_ns
        self._server.broadcast_time(time_ns)

    def _handle(self, notification) -> None:
        if isinstance(notification, Reset):
            self._reset(notification)
        elif isinstance(notification, (Backfill, Message)):
            self._log(notification.message)
        elif isinstance(notification, Refused):
            kind = "latest value" if notification.backfill else "Message"
            self._warn(f"SiL refused the {kind} of {notification.channel!r} at "
                       f"{notification.publication_time_ns} ns (record_index "
                       f"{notification.record_index}, {notification.size_bytes} "
                       f"bytes): {notification.reason}")
        elif isinstance(notification, Commit):
            self._settle()  # the state follows the data it reports
            self._broadcast(notification.state, request_id=notification.state.request_id)
            self._advance_time(notification.state.cursor_ns)
        elif isinstance(notification, StateChanged):
            self._settle()  # "ended" follows the final Messages
            state = notification.state
            if state.status == "failed":
                self._server.publish_status(
                    f"SiL playback failed: {state.diagnostic}", StatusLevel.Error)
            self._broadcast(state, request_id=None)
            self._advance_time(state.cursor_ns)

    def _log(self, message) -> None:
        payload = self._codecs[message.channel].payload(
            message.data, message.publication_time_ns, message.record_index)
        if len(payload) > self._max_message_bytes:
            self._warn(f"SiL refused the Message of {message.channel!r} at "
                       f"{message.publication_time_ns} ns (record_index "
                       f"{message.record_index}): its JSON payload has "
                       f"{len(payload)} bytes, above {self._max_message_bytes}")
            return
        self._channels[message.channel].log(payload,
                                           log_time=message.publication_time_ns)
        with self._changed:
            self._logged += 1
        self._latest_logged = message.publication_time_ns

    def _reset(self, reset: Reset) -> None:
        """Clear the client's history after everything logged before."""
        if not self._settle():
            return
        state = self._playback.status()
        with self._changed:
            seen = self._relay.seek_states_seen
            connection = self._relay.connection
        while True:
            self._broadcast(state, request_id=state.request_id, did_seek=True,
                            position_ns=reset.position_ns)
            if self._wait(lambda: self._relay.seek_states_seen > seen,
                          connection):
                self._advance_time(reset.position_ns, jump=True)
                return
            if not self._live(connection):
                return

    # --- flow control ----------------------------------------------------------

    def _reset_accounting(self) -> None:
        """Forget the entries of a previous client. Barrier numbers keep
        counting, so a late barrier of that client acknowledges nothing."""
        with self._changed:
            self._logged = 0
            self._acked = 0
            self._barriers = {}  # barrier number -> entries logged up to it
            self._barrier_sent_at = 0.0
            self._time = None

    def _acked_floor(self) -> int:
        """Entries known to have left the SDK queue (lock held)."""
        seen = self._relay.barrier_seen
        for barrier in [b for b in self._barriers if b <= seen]:
            self._acked = max(self._acked, self._barriers.pop(barrier))
        return self._acked

    def _unacknowledged(self) -> int:
        with self._changed:
            if not self._relay.connected:
                return 0
            return self._logged - self._acked_floor()

    def _room(self) -> int:
        return self._window - self._unacknowledged()

    def _barrier(self) -> None:
        """Send a barrier unless a recent one is still on its way."""
        with self._changed:
            if not self._relay.connected:
                return
            outstanding = self._next_barrier - 1 > self._relay.barrier_seen
            if outstanding and time.monotonic() - self._barrier_sent_at < _RETRY_S:
                return
            number = self._next_barrier
            self._next_barrier += 1
            self._logged += 1  # the barrier takes a slot of the same queue
            self._barriers[number] = self._logged
            self._barrier_sent_at = time.monotonic()
        self._server.publish_status("SiL delivery barrier", StatusLevel.Info,
                                    id=f"{BARRIER_ID}{number}")

    def _await_acknowledgement(self) -> None:
        with self._changed:
            connection = self._relay.connection
        self._barrier()
        self._wait(lambda: self._room_locked() > 0, connection, timeout=_RETRY_S)

    def _room_locked(self) -> int:
        return self._window - (self._logged - self._acked_floor())

    def _settle(self) -> bool:
        """Wait until every entry logged so far left the SDK queue. False
        when the client went away or the bridge closed."""
        with self._changed:
            connection = self._relay.connection
        while self._live(connection):
            with self._changed:
                if self._logged == self._acked_floor():
                    return True
            self._barrier()
            self._wait(lambda: self._logged == self._acked_floor(), connection,
                       timeout=_RETRY_S)
        return False

    def _wait(self, condition, connection: int, timeout: float = _RETRY_S) -> bool:
        """Wait on the relay until `condition` holds (True), the client of
        `connection` left, the bridge closed or `timeout` passed."""
        with self._changed:
            self._changed.wait_for(
                lambda: condition() or self._closed
                or self._relay.connection != connection
                or not self._relay.connected, timeout)
            return condition() and not self._closed

    def _live(self, connection: int) -> bool:
        with self._changed:
            return (not self._closed and self._relay.connected
                    and self._relay.connection == connection)

    # --- helpers ---------------------------------------------------------------

    def _broadcast(self, state: PlaybackState, *, request_id: str | None,
                   did_seek: bool = False, position_ns: int | None = None) -> None:
        self._server.broadcast_playback_state(_foxglove_state(
            state, did_seek=did_seek, request_id=request_id,
            position_ns=position_ns))

    def _warn(self, message: str) -> None:
        self._server.publish_status(message, StatusLevel.Warning)

    def _channel(self, summary, source_sha256: str):
        codec = self._codecs[summary.name]
        return foxglove.Channel(
            summary.name, message_encoding=interchange.MESSAGE_ENCODING,
            schema=foxglove.Schema(name=summary.schema_name,
                                   encoding=interchange.SCHEMA_ENCODING,
                                   data=codec.json_schema()),
            context=self._context, metadata={
                "sil_profile": interchange.PROFILE,
                "sil_profile_version": str(interchange.PROFILE_VERSION),
                "sil_source_sha256": source_sha256,
                "sil_source_schema": summary.schema_name,
                "sil_source_schema_sha256": summary.schema_sha256,
            })

    def _session_id(self) -> str:
        self._sessions += 1
        return f"{self._playback.status().session_id}-{self._sessions}"


class _Listener(ServerListener):
    """Only playback control requests: the relay's fences carry the
    subscriptions, because the SDK's subscribe callbacks also come from
    clients that bypass the relay."""

    def __init__(self, bridge: Bridge):
        self._bridge = bridge

    def on_playback_control_request(self, request):
        return self._bridge.control(request)


def _current_time(state: PlaybackState) -> int | None:
    return state.seek_ns if state.pending and state.seek_ns is not None \
        else state.cursor_ns


def _foxglove_state(state: PlaybackState, *, did_seek: bool,
                    request_id: str | None,
                    position_ns: int | None = None) -> FoxgloveState:
    now = position_ns if position_ns is not None else _current_time(state)
    return FoxgloveState(status=_STATUS[state.status],
                         current_time=now if now is not None else 0,
                         playback_speed=state.rate, did_seek=did_seek,
                         request_id=request_id)


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host
