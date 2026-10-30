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
the SDK never drops one. The relay forwards with blocking writes: a slow
client slows the relay, the relay stops reading the SDK, barriers stop, and
the adapter stops taking Messages from the controller, which then reports
buffering. A client that accepts nothing for `stall_timeout` seconds is
disconnected.

The relay admits one client at a time and answers a second one with HTTP
409. It removes `Sec-WebSocket-Extensions` from the client's handshake, so
no compression hides the frames that it reads. It uses only the standard
library.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
from collections.abc import Callable

BARRIER_ID = "sil-barrier-"
_MAX_HEADER = 64 * 1024
_TEXT, _BINARY = 0x1, 0x2
_MESSAGE_DATA, _PLAYBACK_STATE = 1, 5
_DID_SEEK_OFFSET = 1 + 1 + 8 + 4  # opcode, status, current_time, speed
_BUSY = (b"HTTP/1.1 409 Conflict\r\nContent-Type: text/plain\r\n"
         b"Connection: close\r\nContent-Length: 39\r\n\r\n"
         b"this SiL bridge serves one client only\n")


class Closed(Exception):
    """A side of the relayed connection closed."""


class Relay:
    """Listen on `host:port`; relay one client at a time to the SDK server
    on loopback `upstream_port`. `on_connect` runs when the SDK accepted
    the client's handshake, `on_disconnect` when that connection ended.
    `changed` is notified whenever a counter or `connected` changes."""

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
                self._busy = False
                self._sockets = []
                self.changed.notify_all()
            if accepted:
                self._on_disconnect()

    def _client_to_server(self, client: socket.socket,
                          upstream: socket.socket) -> None:
        try:
            while True:
                try:
                    data = client.recv(1 << 16)
                except TimeoutError:
                    continue  # an idle client is not a stalled one
                if not data:
                    break
                upstream.sendall(data)
        except OSError:
            pass
        finally:
            _close(upstream)
            _close(client)

    def _server_to_client(self, upstream: socket.socket,
                          client: socket.socket) -> None:
        reader = _Reader(upstream)
        while True:
            head, payload = reader.frame()
            if self._observe(head[0], payload):
                client.sendall(head + payload)

    def _observe(self, first: int, payload: bytes) -> bool:
        """Count what the SDK wrote; False for a barrier, which the client
        does not see."""
        opcode, final = first & 0x0F, bool(first & 0x80)
        if not final:
            return True
        if opcode == _BINARY and payload[:1] == bytes([_PLAYBACK_STATE]) \
                and len(payload) > _DID_SEEK_OFFSET and payload[_DID_SEEK_OFFSET]:
            with self.changed:
                self.seek_states_seen += 1
                self.changed.notify_all()
        elif opcode == _TEXT and BARRIER_ID.encode() in payload[:256]:
            identifier = json.loads(payload).get("id", "")
            if identifier.startswith(BARRIER_ID):
                with self.changed:
                    self.barrier_seen = int(identifier[len(BARRIER_ID):])
                    self.changed.notify_all()
                return False
        return True


class _Reader:
    """Unmasked WebSocket frames from a server socket."""

    def __init__(self, sock: socket.socket):
        self._sock = sock

    def exact(self, count: int) -> bytes:
        chunks, missing = [], count
        while missing:
            data = self._sock.recv(min(missing, 1 << 20))
            if not data:
                raise Closed
            chunks.append(data)
            missing -= len(data)
        return b"".join(chunks)

    def frame(self) -> tuple[bytes, bytes]:
        head = self.exact(2)
        length = head[1] & 0x7F
        if head[1] & 0x80:
            raise ValueError("a server frame is masked")
        if length == 126:
            extension = self.exact(2)
            length = struct.unpack(">H", extension)[0]
            head += extension
        elif length == 127:
            extension = self.exact(8)
            length = struct.unpack(">Q", extension)[0]
            head += extension
        return head, self.exact(length)


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
