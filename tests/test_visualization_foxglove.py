"""Served playback through the Foxglove SDK adapter (issue #349, spec #345
s3 "Headless playback and Foxglove adapter").

Two seams: the `sil playback serve` process, and the WebSocket wire protocol
that a client of the server sees. `FoxgloveClient` speaks that protocol
(subprotocol `foxglove.sdk.v1`) the way the Foxglove application does; no
Foxglove application runs here. Expected deliveries come from a sequential
scan of the source, never from the index or the controller under test.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time

import pytest
from conftest import ROOT
from mcap.reader import make_reader
from visualization_fixture import write_qualification_source, write_source

from sil.visualization import export_recording
from sil.visualization.playback import QueueLimits, open_playback

PYTHON_SRC = ROOT / "python" / "src"
MS = 1_000_000
S = 1_000_000_000


def without_foxglove_sdk(tmp_path) -> dict:
    """An environment in which `import foxglove` fails, as it does when the
    `sil[foxglove]` extra is not installed."""
    blocker = tmp_path / "blocker" / "foxglove"
    blocker.mkdir(parents=True)
    (blocker / "__init__.py").write_text(
        "raise ImportError('foxglove-sdk is not installed')\n")
    path = os.pathsep.join([str(blocker.parent), str(PYTHON_SRC)])
    return dict(os.environ, PYTHONPATH=path)


def sil(args, env, **kwargs):
    return subprocess.run([sys.executable, "-m", "sil.cli", *args], env=env,
                          capture_output=True, text=True, timeout=120, **kwargs)


def test_serve_without_the_foxglove_extra_names_the_extra(tmp_path):
    source = write_qualification_source(tmp_path / "run.mcap")
    proc = sil(["playback", "serve", str(source), "--adapter", "foxglove",
                "--cache-dir", str(tmp_path / "cache")],
               without_foxglove_sdk(tmp_path))
    assert proc.returncode == 2, proc.stderr
    assert "pip install 'sil[foxglove]'" in proc.stderr


def test_export_and_other_commands_work_without_the_foxglove_extra(tmp_path):
    env = without_foxglove_sdk(tmp_path)
    source = write_qualification_source(tmp_path / "run.mcap")
    export = sil(["recording", "export", str(source), "--profile",
                  "visualization-json", "-o", str(tmp_path / "out.mcap")], env)
    assert export.returncode == 0, export.stderr
    assert (tmp_path / "out.mcap.receipt.json").exists()
    channels = sil(["recording", "channels", str(source)], env)
    assert channels.returncode == 0, channels.stderr
    help_text = sil(["playback", "serve", "--help"], env)
    assert help_text.returncode == 0, help_text.stderr
    assert "--listen" in help_text.stdout


# --- the wire seam ---------------------------------------------------------

pytestmark = pytest.mark.filterwarnings(
    "ignore:connect\\(\\) must be used:DeprecationWarning")


@pytest.fixture(scope="module")
def adapter():
    pytest.importorskip("foxglove", reason="needs the sil[foxglove] extra")
    pytest.importorskip("websockets", reason="needs the websockets client")
    from sil.visualization.adapters import foxglove
    return foxglove


PLAYING, PAUSED, BUFFERING, ENDED = 0, 1, 2, 3
PLAY, PAUSE = 0, 1


class FoxgloveClient:
    """A scripted client of the Foxglove WebSocket protocol, subprotocol
    `foxglove.sdk.v1`: it decodes what the server sends, in arrival order,
    and sends subscriptions and playback control requests."""

    def __init__(self, url, subprotocol="foxglove.sdk.v1"):
        from websockets.sync.client import connect
        self.ws = connect(url, subprotocols=[subprotocol], max_size=None,
                          open_timeout=5)
        self.frames = []  # every decoded frame, in arrival order
        self._new = threading.Condition()
        self.channels = {}  # topic -> advertised channel
        self._subscriptions = {}  # subscription id -> topic
        self._next_id = 1
        self.reading = threading.Event()
        self.reading.set()
        self.closed = False
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        from websockets.exceptions import ConnectionClosed
        try:
            while True:
                self.reading.wait()
                frame = self._decode(self.ws.recv())
                with self._new:
                    self.frames.append(frame)
                    self._new.notify_all()
        except ConnectionClosed:
            with self._new:
                self.closed = True
                self._new.notify_all()

    def _decode(self, data):
        if isinstance(data, str):
            message = json.loads(data)
            if message["op"] == "advertise":
                for channel in message["channels"]:
                    self.channels[channel["topic"]] = channel
            return message
        opcode = data[0]
        if opcode == 1:
            sub, log_time = struct.unpack_from("<IQ", data, 1)
            return {"op": "message", "topic": self._subscriptions.get(sub),
                    "log_time": log_time, "payload": json.loads(data[13:])}
        if opcode == 2:
            return {"op": "time", "time": struct.unpack_from("<Q", data, 1)[0]}
        if opcode == 5:
            status, now, speed, did_seek, length = struct.unpack_from(
                "<BQfBI", data, 1)
            return {"op": "playbackState", "status": status, "time": now,
                    "speed": speed, "did_seek": bool(did_seek),
                    "request_id": data[19:19 + length].decode() or None}
        return {"op": f"binary-{opcode}"}

    def wait_for(self, predicate, timeout=10.0):
        """The first frame from now on (or already received, at or after
        index `start`) that matches; fails after `timeout` seconds."""
        deadline = time.monotonic() + timeout
        with self._new:
            index = 0
            while True:
                while index < len(self.frames):
                    if predicate(self.frames[index]):
                        return index, self.frames[index]
                    index += 1
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"no matching frame; got {self.frames[-5:]}"
                self._new.wait(remaining)

    def subscribe(self, *topics):
        subscriptions = []
        for topic in topics:
            sub = self._next_id
            self._next_id += 1
            self._subscriptions[sub] = topic
            subscriptions.append({"id": sub, "channelId": self.channels[topic]["id"]})
        self.ws.send(json.dumps({"op": "subscribe", "subscriptions": subscriptions}))

    def unsubscribe(self, *topics):
        ids = [sub for sub, topic in self._subscriptions.items() if topic in topics]
        self.ws.send(json.dumps({"op": "unsubscribe", "subscriptionIds": ids}))

    def request(self, request_id, command=PAUSE, speed=1.0, seek=None):
        self.ws.send(b"\x03" + struct.pack(
            "<BfBQI", command, speed, seek is not None, seek or 0,
            len(request_id)) + request_id.encode())

    def close(self):
        self.ws.close()


@pytest.fixture
def qualification(tmp_path):
    return write_qualification_source(tmp_path / "run.mcap")


@pytest.fixture
def serve(adapter, tmp_path):
    """Serve a source on an ephemeral loopback port; close it afterwards."""
    bridges = []

    def start(source, queue_limits=None, adjust=lambda playback: playback,
              **options):
        playback = adjust(open_playback(source, cache_dir=tmp_path / "cache",
                                        queue_limits=queue_limits))
        bridge = adapter.Bridge(playback, "127.0.0.1", 0, **options)
        bridges.append(bridge)
        return bridge

    yield start
    for bridge in bridges:
        bridge.close()


def exported_schemas(source, tmp_path):
    """Topic -> (encoding, schema name, schema encoding, schema text) as the
    export writes them."""
    output = tmp_path / "export.mcap"
    export_recording(source, output, profile="visualization-json")
    with open(output, "rb") as stream:
        summary = make_reader(stream).get_summary()
    return {channel.topic: (channel.message_encoding,
                            summary.schemas[channel.schema_id].name,
                            summary.schemas[channel.schema_id].encoding,
                            summary.schemas[channel.schema_id].data.decode())
            for channel in summary.channels.values()}


def exported_messages(source, tmp_path):
    """Every exported Message as (topic, log time, payload), in stored
    order, read back from the export with the mcap reader."""
    output = tmp_path / "messages.mcap"
    export_recording(source, output, profile="visualization-json")
    with open(output, "rb") as stream:
        return [(channel.topic, message.log_time, json.loads(message.data))
                for _, channel, message in make_reader(stream).iter_messages(
                    log_time_order=False)]


def key(entry):
    """(time, record_index) of a received or exported Message."""
    return (entry[1], int(entry[2]["sil"]["record_index"]))


def received(client, start=0):
    return [(f["topic"], f["log_time"], f["payload"])
            for f in client.frames[start:] if f["op"] == "message"]


def latest_at(messages, topics, time_ns):
    found = {}
    for message in messages:
        if message[0] in topics and message[1] <= time_ns:
            if message[0] not in found or key(message) > key(found[message[0]]):
                found[message[0]] = message
    return sorted(found.values(), key=key)


def connect_subscribed(bridge, *topics):
    client = FoxgloveClient(bridge.url)
    client.wait_for(lambda f: f["op"] == "advertise")
    client.subscribe(*(topics or tuple(client.channels)))
    return client


def connect_when_free(bridge, *topics, timeout=5.0):
    """Connect and subscribe as soon as the bridge has let the previous
    client go; until then it answers HTTP 409."""
    from websockets.exceptions import InvalidStatus
    deadline = time.monotonic() + timeout
    while True:
        try:
            return connect_subscribed(bridge, *topics)
        except InvalidStatus:
            assert time.monotonic() < deadline, "the bridge stayed busy"
            time.sleep(0.05)


def test_a_direct_connection_to_the_sdk_port_cannot_affect_playback(
        serve, qualification, tmp_path):
    bridge = serve(qualification)
    client = connect_subscribed(bridge)
    initial_state(client)
    # Any local process can find the SDK server's loopback port; the test
    # reads it from the bridge instead of scanning for it.
    direct = FoxgloveClient(f"ws://127.0.0.1:{bridge._server.port}")
    direct.wait_for(lambda f: f["op"] == "advertise")
    direct.subscribe("counter")
    direct.request("intruder-play", PLAY, speed=10.0, seek=5 * S)
    time.sleep(0.5)
    messages = exported_messages(qualification, tmp_path)
    assert all(f.get("status") != PLAYING for f in client.frames)
    assert received(client) == []  # nothing precedes the first instant
    # The SDK tells the direct client that its request got no answer.
    direct.wait_for(lambda f: f["op"] == "status" and f["level"] == 2)
    direct.close()
    client.request("play", PLAY, speed=10.0)
    client.wait_for(lambda f: f.get("status") == ENDED, timeout=20)
    assert received(client) == sorted(messages, key=key)
    client.close()


def test_the_sdk_refuses_the_legacy_subprotocol(serve, qualification):
    from websockets.exceptions import InvalidStatus
    bridge = serve(qualification)
    with pytest.raises(InvalidStatus):
        FoxgloveClient(bridge.url, subprotocol="foxglove.websocket.v1")


def test_server_advertises_range_time_control_and_the_export_schemas(
        serve, qualification, tmp_path):
    bridge = serve(qualification)
    client = FoxgloveClient(bridge.url)
    _, info = client.wait_for(lambda f: f["op"] == "serverInfo")
    assert {"time", "playbackControl"} <= set(info["capabilities"])
    assert info["dataStartTime"] == {"sec": 0, "nsec": 0}
    assert info["dataEndTime"] == {"sec": 9, "nsec": 990_000_000}
    client.wait_for(lambda f: f["op"] == "advertise")
    advertised = {topic: (c["encoding"], c["schemaName"], c["schemaEncoding"],
                          c["schema"]) for topic, c in client.channels.items()}
    assert advertised == exported_schemas(qualification, tmp_path)
    client.close()


def test_a_new_client_at_the_start_gets_the_whole_first_instant_on_play(
        serve, qualification, tmp_path):
    bridge = serve(qualification)
    client = connect_subscribed(bridge)
    _, state = client.wait_for(lambda f: f["op"] == "playbackState")
    assert (state["status"], state["time"], state["did_seek"]) == (PAUSED, 0, False)
    time.sleep(0.2)
    assert received(client) == []  # nothing precedes the first instant
    client.request("play", PLAY, speed=0.1)
    messages = exported_messages(qualification, tmp_path)
    first_instant = sorted((m for m in messages if m[1] == 0), key=key)
    client.wait_for(lambda f: len(received(client)) >= len(first_instant))
    assert received(client)[:len(first_instant)] == first_instant
    burst = [p["fields"]["id"] for topic, _, p in first_instant if topic == "burst"]
    assert burst == [0, 1, 2]  # the whole equal-time Burst, as Messages
    counter = [p for topic, _, p in first_instant if topic == "counter"]
    assert counter[0]["fields"]["count"] == "9007199254740992"
    client.close()


def initial_state(client):
    return client.wait_for(lambda f: f["op"] == "playbackState")[1]


def test_play_delivers_every_message_in_order_with_its_time_then_ends(
        serve, qualification, tmp_path):
    bridge = serve(qualification)
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play-1", PLAY, speed=10.0)
    _, answer = client.wait_for(lambda f: f.get("request_id") == "play-1")
    assert (answer["status"], answer["speed"]) == (PLAYING, 10.0)
    _, ended = client.wait_for(lambda f: f.get("status") == ENDED, timeout=20)
    assert ended["time"] == 9_990 * MS
    messages = exported_messages(qualification, tmp_path)
    assert received(client) == sorted(messages, key=key)
    times = [f["time"] for f in client.frames if f["op"] == "time"]
    assert times == sorted(times) and times[-1] == 9_990 * MS
    client.close()


def after_last_seek(client, position_ns):
    """Messages after the last did_seek state at `position_ns`."""
    seeks = [i for i, f in enumerate(client.frames) if f["op"] == "playbackState"
             and f["did_seek"] and f["time"] == position_ns]
    assert seeks, "no did_seek state"
    return received(client, seeks[-1] + 1)


def test_seek_backward_while_playing_clears_then_backfills_without_stale_values(
        serve, qualification, tmp_path):
    bridge = serve(qualification)
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play", PLAY, speed=10.0)
    client.wait_for(lambda f: f["op"] == "message" and f["log_time"] > 6 * S)
    client.request("seek", PLAY, speed=10.0, seek=2 * S)
    _, answer = client.wait_for(lambda f: f.get("request_id") == "seek"
                                and not f["did_seek"])
    assert answer["status"] == BUFFERING and answer["time"] == 2 * S
    client.wait_for(lambda f: f.get("status") == ENDED, timeout=20)
    messages = exported_messages(qualification, tmp_path)
    expected = latest_at(messages, set(client.channels), 2 * S) + sorted(
        (m for m in messages if m[1] > 2 * S), key=key)
    assert after_last_seek(client, 2 * S) == expected
    backfill = latest_at(messages, set(client.channels), 2 * S)
    assert "sparse" not in {topic for topic, _, _ in backfill}
    client.close()


def test_repeated_seeks_leave_only_the_last_generation(
        serve, qualification, tmp_path):
    bridge = serve(qualification)
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play", PLAY, speed=10.0)
    client.wait_for(lambda f: f["op"] == "message" and f["log_time"] > 1 * S)
    for number, position in enumerate((8 * S, 1 * S, 6 * S, 3 * S)):
        client.request(f"seek-{number}", PLAY, speed=10.0, seek=position)
    client.wait_for(lambda f: f.get("status") == ENDED, timeout=20)
    messages = exported_messages(qualification, tmp_path)
    expected = latest_at(messages, set(client.channels), 3 * S) + sorted(
        (m for m in messages if m[1] > 3 * S), key=key)
    assert after_last_seek(client, 3 * S) == expected
    client.close()


def test_pause_rate_and_invalid_requests_answer_the_actual_state(
        serve, qualification):
    bridge = serve(qualification)
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play", PLAY, speed=1.0)
    client.wait_for(lambda f: f["op"] == "message" and f["log_time"] > 200 * MS)
    client.request("pause", PAUSE, speed=1.0)
    _, paused = client.wait_for(lambda f: f.get("request_id") == "pause")
    assert paused["status"] == PAUSED
    time.sleep(0.3)
    count = len(received(client))
    time.sleep(0.5)
    assert len(received(client)) == count  # nothing arrives while paused
    client.request("faster", PAUSE, speed=2.0)
    _, faster = client.wait_for(lambda f: f.get("request_id") == "faster")
    assert (faster["status"], faster["speed"], faster["time"]) == (
        PAUSED, 2.0, paused["time"])
    client.request("too-fast", PLAY, speed=50.0)
    _, refused = client.wait_for(lambda f: f.get("request_id") == "too-fast")
    assert (refused["status"], refused["speed"], refused["time"]) == (
        PAUSED, 2.0, paused["time"])
    _, warning = client.wait_for(lambda f: f["op"] == "status"
                                 and "refused" in f["message"])
    assert warning["level"] == 1 and "rate" in warning["message"]
    client.close()


def test_requests_without_an_id_are_applied_and_never_conflict(
        serve, qualification):
    bridge = serve(qualification)
    client = connect_subscribed(bridge)
    initial_state(client)

    client.request("", PAUSE, speed=2.0)
    client.wait_for(lambda f: f.get("speed") == 2.0)
    client.request("", PAUSE, speed=3.0)  # other content, also without an ID
    _, answer = client.wait_for(lambda f: f.get("speed") == 3.0)

    assert (answer["status"], answer["request_id"]) == (PAUSED, None)
    assert not any(f["op"] == "status" and "refused" in f["message"]
                   for f in client.frames)
    client.close()


def write_blobs(path, size, count, period_ns):
    """`count` Messages of `size` bytes (4/3 of that as JSON) every
    `period_ns` on Channel `blobs`; field `n` numbers them."""
    schema = {"b.Blob": {"fields": [{"name": "n", "type": "u32"},
                                    {"name": "blob", "type": "u8", "count": size}]}}
    return write_source(path, schema, {"blobs": "b.Blob"},
                        (("blobs", step * period_ns,
                          {"n": step, "blob": bytes([step % 256]) * size})
                         for step in range(count)))


@pytest.fixture
def blobs(tmp_path):
    """2 s of 80 KB JSON Messages every 10 ms: a client that stops
    reading fills the socket buffers within a second."""
    return write_blobs(tmp_path / "blobs.mcap", 60_000, 200, 10 * MS)


@pytest.fixture
def big_blobs(tmp_path):
    """2 s of 1.3 MB JSON Messages every 50 ms: a few of them exceed the
    socket buffers, so a stalled client leaves Messages in the SDK queue."""
    return write_blobs(tmp_path / "big.mcap", 1_000_000, 40, 50 * MS)


def numbers(entries):
    return [payload["fields"]["n"] for _, _, payload in entries]


def test_a_stalled_client_buffers_and_then_receives_every_message(
        serve, blobs):
    bridge = serve(blobs, backlog=16, queue_limits=QueueLimits(max_messages=32))
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play", PLAY, speed=1.0)
    client.wait_for(lambda f: f["op"] == "message")
    client.reading.clear()
    time.sleep(1.5)  # the source plays on; the client accepts nothing
    client.reading.set()
    client.wait_for(lambda f: f.get("status") == ENDED, timeout=30)
    assert numbers(received(client)) == list(range(200))
    assert any(f.get("status") == BUFFERING for f in client.frames)
    client.close()


class LoggedBytes:
    """Wraps a bridge's SDK channel: records the bridge's unacknowledged
    bytes, including the payload, each time it logs a Message."""

    def __init__(self, bridge, topic):
        self._bridge, self._channel = bridge, bridge._channels[topic]
        self.in_flight = []
        bridge._channels[topic] = self

    def log(self, payload, *, log_time):
        self.in_flight.append(self._bridge._unacknowledged_bytes() + len(payload))
        self._channel.log(payload, log_time=log_time)


def test_unacknowledged_bytes_stay_within_the_window(serve, big_blobs):
    bridge = serve(big_blobs, backlog=1024, window_bytes=3 * 2**20)
    logged = LoggedBytes(bridge, "blobs")
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play", PLAY, speed=10.0)
    client.wait_for(lambda f: f["op"] == "message")
    client.reading.clear()
    time.sleep(0.5)  # a stalled client: acknowledgements stop
    client.reading.set()
    client.wait_for(lambda f: f.get("status") == ENDED, timeout=60)

    assert numbers(received(client)) == list(range(40))
    assert max(logged.in_flight) <= 3 * 2**20
    with pytest.raises(ValueError, match="window_bytes"):
        serve(big_blobs, window_bytes=0)


def seek_after_first_messages_drained(playback, monkeypatch, position_ns):
    """Seek as soon as the adapter has drained its first batch of played
    Messages: the batch is then in its hands, from an old generation."""
    from sil.visualization.playback import Message
    drain, stale = playback.drain, []

    def draining(*args, **kwargs):
        batch = drain(*args, **kwargs)
        if not stale and any(isinstance(n, Message) for n in batch):
            stale.extend(n.message.record_index for n in batch
                         if isinstance(n, Message))
            playback.configure("seek-while-held", position_ns=position_ns,
                               playing=False)
        return batch

    monkeypatch.setattr(playback, "drain", draining)
    return stale


def test_drained_notifications_of_an_old_generation_are_not_sent(
        serve, qualification, tmp_path, monkeypatch):
    stale = []
    bridge = serve(qualification, adjust=lambda p: stale.append(
        seek_after_first_messages_drained(p, monkeypatch, 5 * S)) or p)
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play", PLAY, speed=0.1)
    client.wait_for(lambda f: f["op"] == "playbackState" and f["did_seek"]
                    and f["time"] == 5 * S)
    client.wait_for(lambda f: f["op"] == "playbackState" and not f["did_seek"]
                    and f["time"] == 5 * S and f["status"] == PAUSED)
    time.sleep(0.2)

    (held,) = stale
    assert held  # the seek came while that batch was held
    sent = {int(p["sil"]["record_index"]) for _, _, p in received(client)}
    assert not sent & set(held)
    messages = exported_messages(qualification, tmp_path)
    assert received(client) == latest_at(messages, set(client.channels), 5 * S)
    client.close()


def test_queued_values_from_before_a_seek_never_follow_its_reset(
        serve, big_blobs):
    bridge = serve(big_blobs, backlog=32)
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play", PLAY, speed=1.0)
    client.wait_for(lambda f: f["op"] == "message" and f["log_time"] > 200 * MS)
    client.reading.clear()
    time.sleep(1.0)  # data piles up in the SDK queue and the sockets
    client.request("seek", PLAY, speed=1.0, seek=100 * MS)
    time.sleep(0.3)
    client.reading.set()
    client.wait_for(lambda f: f.get("status") == ENDED, timeout=60)
    assert numbers(after_last_seek(client, 100 * MS)) == list(range(2, 40))
    client.close()


def slow_disconnect(playback, monkeypatch, seconds=0.5):
    """Make the controller's disconnect handling take `seconds`, so that a
    next client can try to connect while it runs."""
    disconnect = playback.disconnect

    def slow():
        time.sleep(seconds)
        return disconnect()

    monkeypatch.setattr(playback, "disconnect", slow)
    return playback


def test_reconnect_starts_a_new_session_paused_with_fresh_backfill(
        serve, qualification, tmp_path, monkeypatch):
    bridge = serve(qualification, adjust=lambda p: slow_disconnect(p, monkeypatch))
    first = connect_subscribed(bridge)
    _, info = first.wait_for(lambda f: f["op"] == "serverInfo")
    initial_state(first)
    first.request("play", PLAY, speed=1.0)
    first.wait_for(lambda f: f["op"] == "message" and f["log_time"] > 300 * MS)
    first.close()
    second = connect_when_free(bridge)
    _, again = second.wait_for(lambda f: f["op"] == "serverInfo")
    assert again["sessionId"] != info["sessionId"]
    reset, state = second.wait_for(lambda f: f["op"] == "playbackState")
    assert state["did_seek"]
    _, committed = second.wait_for(lambda f: f["op"] == "playbackState"
                                   and f["status"] == PAUSED and not f["did_seek"])
    cursor = committed["time"]
    assert cursor > 300 * MS
    time.sleep(0.5)  # paused: nothing beyond the backfill arrives
    messages = exported_messages(qualification, tmp_path)
    assert received(second, reset + 1) == latest_at(messages, set(second.channels),
                                                    cursor)
    second.close()


def test_a_second_client_is_refused_while_one_is_connected(serve, qualification):
    from websockets.exceptions import InvalidStatus
    bridge = serve(qualification)
    first = FoxgloveClient(bridge.url)
    first.wait_for(lambda f: f["op"] == "serverInfo")
    with pytest.raises(InvalidStatus) as refused:
        FoxgloveClient(bridge.url)
    assert refused.value.response.status_code == 409
    first.close()
    deadline = time.monotonic() + 5
    while True:  # the bridge frees itself when the first connection ends
        try:
            second = FoxgloveClient(bridge.url)
            break
        except InvalidStatus:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    second.wait_for(lambda f: f["op"] == "serverInfo")
    second.close()


def test_subscription_changes_backfill_added_and_stop_removed_topics(
        serve, qualification, tmp_path):
    bridge = serve(qualification)
    client = connect_subscribed(bridge, "counter")
    initial_state(client)
    client.request("seek", PAUSE, speed=1.0, seek=4 * S)
    client.wait_for(lambda f: f.get("request_id") == "seek" and f["status"] == PAUSED
                    and not f["did_seek"] and f["time"] == 4 * S)
    start = len(client.frames)
    client.subscribe("sparse", "vehicle/speed")
    client.unsubscribe("counter")
    client.wait_for(lambda f: f["op"] == "message" and f["topic"] == "vehicle/speed")
    client.request("play", PLAY, speed=10.0)
    client.wait_for(lambda f: f.get("status") == ENDED, timeout=20)
    messages = exported_messages(qualification, tmp_path)
    topics = {"sparse", "vehicle/speed"}
    expected = latest_at(messages, topics, 4 * S) + sorted(
        (m for m in messages if m[0] in topics and m[1] > 4 * S), key=key)
    assert [m for m in received(client, start) if m[0] != "counter"] == expected
    assert [m for m in received(client, start) if m[0] == "counter"] == []
    client.close()


def test_oversize_payloads_are_reported_and_never_sent(
        serve, qualification, tmp_path):
    messages = exported_messages(qualification, tmp_path)
    size = {topic: max(len(json.dumps(p, separators=(",", ":")))
                       for t, _, p in messages if t == topic)
            for topic in {t for t, _, _ in messages}}
    limit = max(s for topic, s in size.items() if topic != "arrays")
    assert min(len(json.dumps(p, separators=(",", ":")))
               for t, _, p in messages if t == "arrays") > limit
    bridge = serve(qualification, max_message_bytes=limit)
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play", PLAY, speed=10.0)
    client.wait_for(lambda f: f.get("status") == ENDED, timeout=20)
    assert "arrays" not in {topic for topic, _, _ in received(client)}
    warnings = [f for f in client.frames if f["op"] == "status"
                and "'arrays'" in f["message"]]
    arrays = [m for m in messages if m[0] == "arrays"]
    assert len(warnings) == len(arrays)  # the backfill at 0 and each later one
    assert all(f["level"] == 1 for f in warnings)
    client.close()


def file_sha256(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_serving_and_seeking_leave_the_source_unchanged(serve, qualification):
    beside = lambda: sorted(qualification.parent.glob("run*"))  # noqa: E731
    before = (file_sha256(qualification), beside())
    bridge = serve(qualification)
    client = connect_subscribed(bridge)
    initial_state(client)
    client.request("play", PLAY, speed=10.0)
    client.request("seek", PLAY, speed=10.0, seek=7 * S)
    client.wait_for(lambda f: f.get("status") == ENDED, timeout=20)
    client.close()
    bridge.close()
    assert (file_sha256(qualification), beside()) == before


def test_sources_without_a_foxglove_playback_range_are_refused(
        adapter, tmp_path):
    event = {"e.Event": {"fields": [{"name": "value", "type": "u64"}]}}
    empty = write_source(tmp_path / "empty.mcap", event, {"e": "e.Event"}, [])
    late = write_source(tmp_path / "late.mcap", event, {"e": "e.Event"},
                        [("e", 0, {"value": 1}), ("e", 2**32 * S, {"value": 2})])
    for source, reason in ((empty, "no Messages"), (late, "2^32 s")):
        proc = sil(["playback", "serve", str(source), "--listen", "127.0.0.1:0",
                    "--cache-dir", str(tmp_path / "cache")],
                   dict(os.environ, PYTHONPATH=str(PYTHON_SRC)))
        assert proc.returncode == 2, proc.stderr
        assert reason in proc.stderr


def test_the_serve_command_prints_its_address_and_stops_on_sigterm(
        adapter, qualification, tmp_path):
    import signal
    process = subprocess.Popen(
        [sys.executable, "-m", "sil.cli", "playback", "serve", str(qualification),
         "--adapter", "foxglove", "--listen", "127.0.0.1:0",
         "--cache-dir", str(tmp_path / "cache")],
        env=dict(os.environ, PYTHONPATH=str(PYTHON_SRC)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline()
        assert " at ws://127.0.0.1:" in line, process.stderr.read()
        client = FoxgloveClient(line.rsplit(" at ", 1)[1].strip())
        _, info = client.wait_for(lambda f: f["op"] == "serverInfo")
        assert "playbackControl" in info["capabilities"]
        client.close()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=20) == 0
    finally:
        process.kill()
        process.wait()


def test_a_client_stalled_past_the_timeout_is_disconnected_and_playback_pauses(
        serve, big_blobs):
    bridge = serve(big_blobs, backlog=32, stall_timeout=1.0)
    stalled = connect_subscribed(bridge)
    initial_state(stalled)
    stalled.request("play", PLAY, speed=1.0)
    stalled.wait_for(lambda f: f["op"] == "message")
    stalled.reading.clear()
    deadline = time.monotonic() + 15
    while True:  # the relay gives up on the client
        try:
            fresh = connect_subscribed(bridge)
            break
        except Exception:
            assert time.monotonic() < deadline
            time.sleep(0.2)
    _, state = fresh.wait_for(lambda f: f["op"] == "playbackState")
    assert state["did_seek"]
    _, committed = fresh.wait_for(lambda f: f["op"] == "playbackState"
                                  and not f["did_seek"])
    assert committed["status"] == PAUSED
    fresh.close()
