"""A loopback relay in front of the Foxglove SDK WebSocket server.

Why it exists (docs/visualization.md "Compatibility matrix"): foxglove-sdk
0.30.3 queues data for a client in a bounded queue that drops the oldest
Message when it is full, and tells the program nothing. It also sends its
data queue and its control queue (playback state, time) in no fixed order.
The relay sits between the client and the SDK server and reports what the
SDK actually wrote, in order:

- `barrier_seen`: the number of the last barrier status that the SDK wrote.
  A barrier goes through the same queue as the Messages, so all Messages
  logged before it are out of that queue. The relay does not forward
  barriers to the client.
- `seek_states_seen`: how many playback states with `did_seek` it wrote.

The adapter keeps fewer Messages unacknowledged than the SDK queue holds, so
the SDK never drops a Message. The relay forwards with blocking writes: a slow
client slows the relay, the relay stops reading the SDK, barriers stop, and
the adapter stops taking Messages from the controller, which then reports
buffering. A client that accepts nothing for `stall_timeout` seconds is
disconnected.

Access: the SDK server listens on a loopback port that any local process
can reach, and the SDK does not tell its listener which client sent a
playback control request. So the relay gives each playback control request
of its client a new one-time token as request ID, and the adapter acts only
on a request whose token it can `claim`. After each subscribe or
unsubscribe, the relay sends a fence: a playback control request whose
token claims the client's set of subscribed channel IDs. The SDK handles
the messages of one client in order, so when the fence arrives, the SDK
has registered the subscription. The relay does not forward the answer to
a fence to the client.

The relay admits one client at a time. It answers a second one with HTTP
409 until it has handled the disconnect of the first one. It removes
`Sec-WebSocket-Extensions` from the client's handshake, so no compression
hides the frames that it reads. It uses only the standard library.
"""

from __future__ import annotations

import json
import secrets
import socket
import struct
import threading
from collections.abc import Callable
from typing import NamedTuple

BARRIER_ID = "sil-barrier-"
_MAX_HEADER = 64 * 1024
_CONTINUATION, _TEXT, _BINARY, _CONTROL = 0x0, 0x1, 0x2, 0x8
_PLAYBACK_CONTROL = 3  # client to server
_PLAYBACK_STATE = 5  # server to client
_DID_SEEK_OFFSET = 1 + 1 + 8 + 4  # opcode, status, current_time, speed
# Both playback messages have a u32 length and the request ID at these offsets.
_REQUEST_ID_LENGTH_OFFSET = _DID_SEEK_OFFSET + 1
_REQUEST_ID_OFFSET = _REQUEST_ID_LENGTH_OFFSET + 4
_FENCE = struct.pack("<BBfBQ", _PLAYBACK_CONTROL, 1, 1.0, 0, 0)  # Pause, no seek
_BUSY = (b"HTTP/1.1 409 Conflict\r\nContent-Type: text/plain\r\n"
         b"Connection: close\r\nContent-Length: 39\r\n\r\n"
         b"this SiL bridge serves one client only\n")


class Closed(Exception):
    """A side of the relayed connection closed."""


class ClientRequest(NamedTuple):
    """A playback control request of the relay's client, with its own ID."""

    request_id: str


class Subscription(NamedTuple):
    """The channel IDs that the relay's client subscribes to."""

    channel_ids: frozenset[int]


class Relay:
    """Listen on `host:port`; relay one client at a time to the SDK server
    on loopback `upstream_port`. `on_connect` runs when the SDK accepted
    the client's handshake, `on_disconnect` when that connection ended; the
    next client is admitted after `on_disconnect` returned. `changed` is
    notified whenever a counter or `connected` changes."""

    def __init__(self, host: str, port: int, upstream_port: int, *,
                 on_connect: Callable[[], None],
                 on_disconnect: Callable[[], None],
                 changed: threading.Condition | None = None,
                 stall_timeout: float = 30.0):
        self._upstream = ("127.0.0.1", upstream_port)
        self._on_connect, self._on_disconnect = on_connect, on_disconnect
        self._stall_timeout = stall_timeout
        self.changed = changed or threading.Condition()
        self.connected = False
        self.connection = 0
        self.barrier_seen = -1
        self.seek_states_seen = 0
        self._busy = False
        self._closed = False
        self._sockets: list[socket.socket] = []
        self._tokens: dict[str, ClientRequest | Subscription] = {}
        self._answers: dict[str, str | None] = {}  # token -> answer request ID
        self._listener = socket.create_server((host, port))
        self.port = self._listener.getsockname()[1]
        self._accepting = threading.Thread(target=self._accept, daemon=True,
                                           name="sil-relay-accept")
        self._accepting.start()

    def close(self) -> None:
        with self.changed:
            self._closed = True
            sockets = list(self._sockets)
        _close(self._listener)
        for sock in sockets:
            _close(sock)
        self._accepting.join(timeout=5)

    def claim(self, token: str) -> ClientRequest | Subscription | None:
        """What the request with ID `token` stands for, once. None for a
        request that the relay did not send on its current connection."""
        with self.changed:
            return self._tokens.pop(token, None)

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return  # closed
            with self.changed:
                refuse = self._busy or self._closed
                if not refuse:
                    self._busy = True
                    self._sockets = [client]
            if refuse:
                _refuse(client)
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True,
                             name="sil-relay-connection").start()

    def _serve(self, client: socket.socket) -> None:
        upstream = None
        accepted = False
        try:
            client.settimeout(10)  # a handshake must not hold the bridge
            request, rest = _read_head(client)
            upstream = socket.create_connection(self._upstream, timeout=10)
            upstream.settimeout(None)
            with self.changed:
                if self._closed:
                    raise Closed
                self._sockets.append(upstream)
            upstream.sendall(_without_extensions(request) + rest)
            response, rest = _read_head(upstream)
            client.sendall(response + rest)
            if not response.startswith(b"HTTP/1.1 101"):
                return
            client.settimeout(self._stall_timeout)  # bounds a stalled send
            accepted = True
            with self.changed:
                self.connection += 1
                self.connected = True
                self.barrier_seen = -1
                self.seek_states_seen = 0
                self._forget_tokens()
                self.changed.notify_all()
            self._on_connect()
            upward = threading.Thread(target=self._client_to_server,
                                      args=(client, upstream), daemon=True,
                                      name="sil-relay-upward")
            upward.start()
            self._server_to_client(upstream, client)
        except (Closed, OSError, ValueError):
            pass
        finally:
            _close(client)
            if upstream is not None:
                _close(upstream)
            with self.changed:
                self.connected = False
                self._forget_tokens()
                self.changed.notify_all()
            try:
                if accepted:  # before a next client can subscribe
                    self._on_disconnect()
            finally:
                with self.changed:
                    self._busy = False
                    self._sockets = []

    def _forget_tokens(self) -> None:
        """Requests of an earlier connection claim nothing (lock held)."""
        self._tokens.clear()
        self._answers.clear()

    def _client_to_server(self, client: socket.socket,
                          upstream: socket.socket) -> None:
        reader = _Reader(client)
        subscriptions: dict[int, int] = {}  # subscription ID -> channel ID
        fragments: list[bytes] = []
        message_opcode = _BINARY
        try:
            while True:
                head, payload = reader.frame(masked=True)
                opcode, final = head[0] & 0x0F, bool(head[0] & 0x80)
                if opcode & _CONTROL:  # ping, pong, close: as they are
                    upstream.sendall(head + payload)
                    continue
                if opcode != _CONTINUATION:
                    message_opcode = opcode
                fragments.append(_unmask(head[-4:], payload))
                if not final:
                    continue
                message, fragments = b"".join(fragments), []
                for opcode, data in self._rewrite(message_opcode, message,
                                                  subscriptions):
                    upstream.sendall(_frame(opcode, data, masked=True))
        except (Closed, OSError, ValueError):
            pass
        finally:
            _close(upstream)
            _close(client)

    def _rewrite(self, opcode: int, data: bytes,
                 subscriptions: dict[int, int]) -> list[tuple[int, bytes]]:
        """The messages that the SDK gets for one message of the client."""
        if opcode == _BINARY and data[:1] == bytes([_PLAYBACK_CONTROL]) \
                and len(data) >= _REQUEST_ID_OFFSET:
            try:
                request_id = _read_request_id(data).decode()
            except UnicodeDecodeError:
                return [(opcode, data)]  # unclaimed: the SDK refuses it
            token = self._token(ClientRequest(request_id), request_id)
            return [(opcode, _with_request_id(data, token))]
        if opcode == _TEXT and _track(data, subscriptions):
            fence = self._token(Subscription(frozenset(subscriptions.values())),
                                None)
            return [(opcode, data), (_BINARY, _FENCE + _request_id(fence))]
        return [(opcode, data)]

    def _token(self, meaning: ClientRequest | Subscription,
               answer_id: str | None) -> str:
        """A new token for `meaning`. The SDK answers with the token as
        request ID; the client gets that answer with `answer_id`, or not at
        all for None."""
        token = secrets.token_hex(16)
        with self.changed:
            self._tokens[token] = meaning
            self._answers[token] = answer_id
        return token

    def _server_to_client(self, upstream: socket.socket,
                          client: socket.socket) -> None:
        reader = _Reader(upstream)
        while True:
            head, payload = reader.frame()
            forwarded = self._observe(head, payload)
            if forwarded is not None:
                client.sendall(forwarded)

    def _observe(self, head: bytes, payload: bytes) -> bytes | None:
        """Count what the SDK wrote and return the frame for the client:
        None for a barrier or the answer to a fence, and an answer with the
        request ID that the client sent."""
        opcode, final = head[0] & 0x0F, bool(head[0] & 0x80)
        if not final:
            return head + payload
        if opcode == _BINARY and payload[:1] == bytes([_PLAYBACK_STATE]) \
                and len(payload) >= _REQUEST_ID_OFFSET:
            token = _read_request_id(payload).decode(errors="replace")
            with self.changed:
                if payload[_DID_SEEK_OFFSET]:
                    self.seek_states_seen += 1
                    self.changed.notify_all()
                if token not in self._answers:
                    return head + payload
                answer_id = self._answers.pop(token)
            if answer_id is None:
                return None
            return _frame(opcode, _with_request_id(payload, answer_id))
        if opcode == _TEXT and BARRIER_ID.encode() in payload[:256]:
            identifier = json.loads(payload).get("id", "")
            if identifier.startswith(BARRIER_ID):
                with self.changed:
                    self.barrier_seen = int(identifier[len(BARRIER_ID):])
                    self.changed.notify_all()
                return None
        return head + payload


class _Reader:
    """WebSocket frames from a socket: masked from a client, unmasked from
    a server. A read timeout is no error: an idle client is not a stalled
    one."""

    def __init__(self, sock: socket.socket):
        self._sock = sock

    def exact(self, count: int) -> bytes:
        chunks, missing = [], count
        while missing:
            try:
                data = self._sock.recv(min(missing, 1 << 20))
            except TimeoutError:
                continue
            if not data:
                raise Closed
            chunks.append(data)
            missing -= len(data)
        return b"".join(chunks)

    def frame(self, *, masked: bool = False) -> tuple[bytes, bytes]:
        """The frame head (with the mask key, if any) and its payload, as
        received."""
        head = self.exact(2)
        length = head[1] & 0x7F
        if bool(head[1] & 0x80) != masked:
            raise ValueError("a frame has the wrong masking for its side")
        if length == 126:
            extension = self.exact(2)
            length = struct.unpack(">H", extension)[0]
            head += extension
        elif length == 127:
            extension = self.exact(8)
            length = struct.unpack(">Q", extension)[0]
            head += extension
        if masked:
            head += self.exact(4)
        return head, self.exact(length)


def _unmask(key: bytes, payload: bytes) -> bytes:
    if not payload:
        return payload
    mask = (key * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload) ^ int.from_bytes(mask)).to_bytes(len(payload))


def _frame(opcode: int, data: bytes, *, masked: bool = False) -> bytes:
    """One final frame. A client frame gets a zero mask key: on loopback,
    masking protects nothing, and a zero key leaves the payload as it is."""
    mask = 0x80 if masked else 0
    length = len(data)
    if length < 126:
        head = bytes([0x80 | opcode, mask | length])
    elif length < 1 << 16:
        head = bytes([0x80 | opcode, mask | 126]) + struct.pack(">H", length)
    else:
        head = bytes([0x80 | opcode, mask | 127]) + struct.pack(">Q", length)
    return head + (bytes(4) if masked else b"") + data


def _request_id(request_id: str) -> bytes:
    """The u32 length and the bytes of a request ID."""
    encoded = request_id.encode()
    return struct.pack("<I", len(encoded)) + encoded


def _read_request_id(payload: bytes) -> bytes:
    """The request ID of a playback control request or playback state."""
    (length,) = struct.unpack_from("<I", payload, _REQUEST_ID_LENGTH_OFFSET)
    return payload[_REQUEST_ID_OFFSET:_REQUEST_ID_OFFSET + length]


def _with_request_id(payload: bytes, request_id: str) -> bytes:
    end = _REQUEST_ID_OFFSET + len(_read_request_id(payload))
    return (payload[:_REQUEST_ID_LENGTH_OFFSET] + _request_id(request_id)
            + payload[end:])


def _track(text: bytes, subscriptions: dict[int, int]) -> bool:
    """Apply a subscribe or unsubscribe of the client to `subscriptions`
    (subscription ID -> channel ID). False for any other message; the SDK
    refuses a malformed one."""
    try:
        message = json.loads(text)
        if message.get("op") == "subscribe":
            for entry in message["subscriptions"]:
                subscriptions[int(entry["id"])] = int(entry["channelId"])
            return True
        if message.get("op") == "unsubscribe":
            for identifier in message["subscriptionIds"]:
                subscriptions.pop(int(identifier), None)
            return True
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return False


def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    """An HTTP head up to its blank line, and the bytes read after it."""
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > _MAX_HEADER:
            raise ValueError("HTTP head too large")
        chunk = sock.recv(4096)
        if not chunk:
            raise Closed
        data += chunk
    end = data.index(b"\r\n\r\n") + 4
    return data[:end], data[end:]


def _without_extensions(request: bytes) -> bytes:
    lines = request.split(b"\r\n")
    return b"\r\n".join(line for line in lines if not line.lower().startswith(
        b"sec-websocket-extensions:"))


def _refuse(client: socket.socket) -> None:
    try:
        client.settimeout(2)
        _read_head(client)
        client.sendall(_BUSY)
    except (Closed, OSError, ValueError):
        pass
    finally:
        _close(client)


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()
