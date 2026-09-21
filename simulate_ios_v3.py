#!/usr/bin/env python3
"""Synthetic Face Motion v3 sender: test UDP/TCP without an iPhone.

Keep face_motion_v3.py in the SAME folder. No third-party packages are needed.
All schema ACK/retry logic is included in this file. Only binary codecs and
protocol constants are imported from face_motion_v3.py.

Same-PC test (two terminals, after changing to this folder):
  1. python simulate_ios_v3.py --transport udp --port 51083 --count 52 --change-after 0
  2. python face_motion_v3.py --host 127.0.0.1 --transport udp --port 51083
For TCP, replace udp with tcp in BOTH commands. Stop each with Ctrl+C.
51083 is a local test override, not an iOS standard port. The two processes
must not bind the same address/UDP port. Default bind address is loopback.

Values and names are synthetic, not camera/ARKit measurements. The default
small example has 3 fields and adds one after 30 frames; --change-after 0
turns that change off. --count 52 does not mean the 52 standard ARKit names.
This is a test tool, not a production iOS implementation.
"""
from __future__ import annotations
from collections import deque
import argparse
import logging
import math
import secrets
import socket
import threading
import time
from typing import Sequence
import face_motion_v3 as v3

LOG = logging.getLogger("v3_simulator")


# SCHEMA delivery is included here; no schema_delivery.py is needed.
# The caller validates peer/session and retains only the latest values.
# All times below are monotonic seconds.
SCHEMA_RETRY_SECONDS = 1.0
SCHEMA_ACK_TIMEOUT_SECONDS = 10.0
SCHEMA_REFRESH_SECONDS = 10.0


class SchemaDelivery:
    """UDP waits for matching ACK; TCP waits only for the SCHEMA write/queue.

    Changes during an existing UDP ACK wait supersede the pending schema, but
    do not restart its deadline or the per-peer resend rate limiter.
    """
    def __init__(self, transport: str) -> None:
        if transport not in ("udp", "tcp"):
            raise ValueError("transport must be udp or tcp")
        self.transport = transport
        self.schema_id = 0
        self.wire_bytes = b""
        self.ready_schema_id = 0
        self.wait_started: float | None = None
        self.last_sent = -math.inf
        self.sent_current = False
        self.expired = False

    @staticmethod
    def _time(now: float) -> None:
        if not math.isfinite(now):
            raise ValueError("time must be finite monotonic seconds")

    def install(self, schema_id: int, wire_bytes: bytes, now: float) -> bool:
        """Install a new immutable schema. Return False for an identical repeat."""
        self._time(now)
        if self.expired:
            raise RuntimeError("expired delivery state requires a new session")
        if type(schema_id) is not int or not 1 <= schema_id <= 0xFFFFFFFF:
            raise ValueError("schema_id must be a nonzero UInt32")
        if not isinstance(wire_bytes, bytes) or not wire_bytes:
            raise ValueError("wire_bytes must be nonempty immutable bytes")
        if schema_id < self.schema_id:
            raise ValueError("schema id cannot go backwards")
        if schema_id == self.schema_id:
            if wire_bytes != self.wire_bytes:
                raise ValueError("same schema id reused with different contents")
            return False
        self.schema_id, self.wire_bytes = schema_id, wire_bytes
        self.ready_schema_id = 0
        self.sent_current = False
        if self.transport == "udp" and self.wait_started is None:
            self.wait_started = now
        # Keep last_sent: rapid settings changes may not cause an unbounded burst.
        return True

    def check_timeout(self, now: float) -> bool:
        """Latch failure; even PING or a late ACK cannot revive expired state."""
        self._time(now)
        if (self.transport == "udp" and self.wait_started is not None and
                now - self.wait_started >= SCHEMA_ACK_TIMEOUT_SECONDS):
            self.expired = True
            self.ready_schema_id = 0
        return self.expired

    def due(self, now: float, *, requested: bool = False) -> bool:
        """True when caller should send the current complete cached SCHEMA.

        Timer, HELLO repeats and GET_SCHEMA share a 1/second UDP limiter.
        A duplicate request does NOT reset ready_schema_id or wait_started.
        """
        if not self.schema_id or self.check_timeout(now):
            return False
        if self.transport == "tcp":
            return not self.sent_current or (requested and now - self.last_sent >= SCHEMA_RETRY_SECONDS)
        interval = (SCHEMA_RETRY_SECONDS if requested or not self.can_send(self.schema_id)
                    else SCHEMA_REFRESH_SECONDS)
        return now - self.last_sent >= interval

    def mark_sent(self, schema_id: int, now: float) -> None:
        """Call once when a complete SCHEMA send batch is committed to transport.

        An intentionally lost UDP packet still counts as a send attempt. This is
        NOT a delivery receipt. TCP must enqueue the whole SCHEMA before FRAME.
        """
        self._time(now)
        if schema_id != self.schema_id or not self.schema_id:
            raise ValueError("cannot mark a superseded schema as sent")
        if self.check_timeout(now):
            raise RuntimeError("schema ACK deadline expired")
        self.last_sent, self.sent_current = now, True
        if self.transport == "tcp":
            self.ready_schema_id = schema_id

    def acknowledge(self, schema_id: int, now: float) -> bool:
        """Endpoint/session validation is the caller's responsibility.

        Never accept an old, future, not-yet-sent or timed-out schema ACK.
        Duplicate matching ACKs are idempotent; they do not restart sequences.
        """
        if self.transport != "udp" or self.check_timeout(now):
            return False
        if schema_id != self.schema_id or not self.sent_current:
            return False
        self.ready_schema_id = schema_id
        self.wait_started = None
        return True

    def can_send(self, schema_id: int) -> bool:
        """Only the currently installed and synchronized layout may be sent."""
        return (not self.expired and schema_id != 0 and
                schema_id == self.schema_id == self.ready_schema_id)

class Simulator:
    def __init__(self, *, transport: str = "udp", host: str = "127.0.0.1", port: int | None = None,
                 app: str = "Facemotion3d", count: int = 3, change_after: int = 30,
                 reverse_fragments: bool = False, drop_first_schema: bool = False,
                 drop_first_schema_part: int | None = None, max_udp_size: int = 1200,
                 push_target: tuple[str, int] | None = None, push_delay: float = 0.0,
                 mute_frames: bool = False, mute_after: int = 0) -> None:
        if transport not in ("udp", "tcp") or not 3 <= count <= 4095 or app not in v3.APPS:
            raise ValueError("invalid simulator configuration")
        if not 576 <= max_udp_size <= 1200:
            raise ValueError("invalid UDP limit")
        if port is None:
            port = v3.default_ports(app, transport)[0]
        self.transport, self.app = transport, app
        self.push_target, self.push_delay = push_target, push_delay
        self.mute_frames, self.mute_after = mute_frames, mute_after
        self.drop_first_schema, self.drop_first_schema_part = drop_first_schema, drop_first_schema_part
        self.schema_attempts = 0
        self.max_udp_size = max_udp_size
        self._hello_settings = None
        self.count, self.change_after, self.reverse_fragments = count, change_after, reverse_fragments
        kind = socket.SOCK_DGRAM if transport == "udp" else socket.SOCK_STREAM
        self.sock = socket.socket(socket.AF_INET, kind)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.02)
        if transport == "tcp":
            self.sock.listen(1)
        self.peer = None
        self.connection: socket.socket | None = None
        self.session_id = 0
        self.nonce = 0
        self.schema_id = 1
        self.sequence = 0
        self.fps = 60
        self.udp_size = 1200
        self.last_control = 0.0
        self.last_schema = 0.0
        self.delivery = SchemaDelivery(transport)
        self.changed = False
        self.frames_sent = 0
        self.controls: deque[int] = deque(maxlen=4096)
        self.messages_sent: deque[int] = deque(maxlen=4096)
        self._next_frame = 0.0
        self._framer = v3.TCPFramer()
        self._make_schema()

    def _make_schema(self, now: float | None = None) -> None:
        names = ["jawOpen", "eyeBlinkLeft", "myCustomSmile"]
        names += [f"custom_{i:04d}" for i in range(self.count - 3)]
        if self.changed:
            names += ["myCustomAngry"]
        self.schema_data = v3.schema_payload(names, app=self.app)
        self.schema = v3.Schema.parse(self.schema_data, self.session_id or 1, self.schema_id)
        self.schema_wire = (v3.schema_message_payload(self.schema_data, client_nonce=self.nonce,
                            actual_fps=self.fps, max_udp_size=self.udp_size) if self.session_id else b"")
        if self.session_id:
            self.delivery.install(self.schema_id, self.schema_wire,
                                  time.monotonic() if now is None else now)

    def _send(self, kind: int, payload: bytes = b"", *, schema_id: int = 0,
              sequence: int = 0, flags: int = 0, token: int = 0) -> None:
        raws = v3.encode_message(kind, payload, session_id=self.session_id,
                                schema_id=schema_id, sequence=sequence, flags=flags,
                                token=token, udp_size=self.udp_size if self.transport == "udp" else None)
        if self.reverse_fragments and self.transport == "udp":
            raws.reverse()
        for raw in raws:
            if (kind == v3.SCHEMA and self.schema_attempts == 1 and
                    (self.drop_first_schema or
                     v3.decode_packet(raw).part_index == self.drop_first_schema_part)):
                continue
            self.messages_sent.append(kind)
            if self.transport == "udp":
                if self.peer:
                    self.sock.sendto(raw, self.peer)
            elif self.connection:
                self.connection.sendall(raw)

    def _send_schema(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.schema_attempts += 1
        self._send(v3.SCHEMA, self.schema_wire, schema_id=self.schema_id)
        self.delivery.mark_sent(self.schema_id, now)
        self.last_schema = now

    def _reply_error(self, nonce: int, reason: bytes, peer=None) -> None:
        raw = v3.encode_message(v3.ERROR, reason, session_id=nonce)[0]
        self.messages_sent.append(v3.ERROR)
        if self.transport == "udp" and peer is not None:
            self.sock.sendto(raw, peer)
        elif self.connection is not None:
            self.connection.sendall(raw)

    def _control(self, packet: v3.Packet, peer=None) -> None:
        self.controls.append(packet.kind)
        now = time.monotonic()
        if packet.kind == v3.HELLO:
            fps, size, revision = v3.HELLO_BODY.unpack(packet.payload)
            if revision != v3.CONTRACT_REVISION:
                self._reply_error(packet.session_id, b"UNSUPPORTED_CONTRACT: requires 4", peer)
                return
            if not 1 <= fps <= 60 or not 576 <= size <= 1200:
                self._reply_error(packet.session_id, b"INVALID_HELLO", peer)
                return
            if self.session_id:
                if ((peer is not None and self.peer != peer) or self.nonce != packet.session_id):
                    self._reply_error(packet.session_id, b"BUSY", peer)
                    return
                if self._hello_settings != (fps, size, revision):
                    self._reply_error(packet.session_id, b"HELLO_SETTINGS_CHANGED", peer)
                    return
                # Same request is idempotent: do not reset the frame/schema counters.
                self.last_control = now
                if self.delivery.due(now, requested=True):
                    self._send_schema(now)
                return
            self.peer = peer
            self.session_id = secrets.randbits(64) or 1
            self.nonce = packet.session_id
            self._hello_settings = (fps, size, revision)
            self.fps, self.udp_size = fps, min(size, self.max_udp_size)
            self.schema_id, self.sequence, self.frames_sent = 1, 0, 0
            self.changed = False
            self.schema_attempts = 0
            self.delivery = SchemaDelivery(self.transport)
            self._make_schema(now)
            # SCHEMA is still the ONLY positive start response.
            # UDP FRAME is now gated by a matching SCHEMA_ACK.
            self._send_schema(now)
            self.last_control = now
            self._next_frame = now
            return
        if packet.session_id != self.session_id or (peer is not None and peer != self.peer):
            return
        if packet.kind == v3.GET_SCHEMA:
            self.last_control = now
            # Send current schema for a retired requested ID, never roll back.
            if self.delivery.due(now, requested=True):
                self._send_schema(now)
        elif packet.kind == v3.SCHEMA_ACK:
            self.last_control = now
            self.delivery.acknowledge(packet.schema_id, now)
        elif packet.kind == v3.PING:
            self.last_control = now
            self._send(v3.PONG, sequence=packet.sequence)
        elif packet.kind == v3.STOP:
            self.session_id = 0
            self.peer = None
        else:
            raise v3.ProtocolError("unexpected client control")

    def _tick(self) -> None:
        now = time.monotonic()
        if not self.session_id:
            return
        # Independent of PING/HELLO: control traffic cannot keep WAIT_ACK alive forever.
        if self.delivery.check_timeout(now):
            self._send(v3.ERROR, b"SCHEMA_ACK_TIMEOUT")
            self.session_id = 0
            self.peer = None
            return
        if now - self.last_control >= 10:
            self.session_id = 0
            self.peer = None
            return
        # A real app supplies a new immutable snapshot at a settings change.
        # This simulator appends one custom channel once, after change_after frames.
        if self.change_after > 0 and not self.changed and self.frames_sent >= self.change_after:
            self.changed = True
            self.schema_id += 1
            self._make_schema(now)
        if self.delivery.due(now):
            self._send_schema(now)
        if not self.delivery.can_send(self.schema_id):
            return  # neither old-layout nor new-layout frames are queued during UDP WAIT_ACK
        if self.mute_frames or (self.mute_after and self.frames_sent >= self.mute_after):
            return
        if now < self._next_frame:
            return
        self._next_frame = now + 1.0 / self.fps
        values = [self.frames_sent % 101, 0, -25 if self.app == "Facemotion3d" else 25]
        values += [0] * (len(self.schema.blend_names) - 3)
        if self.changed:
            values[-1] = 75
        pose = [1.25, -2.5, 3.75, 0.1, -0.2, 0.3, 4., 5., 6., 7., 8., 9.]
        payload = v3.frame_payload(self.schema, values, pose)
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        flags = 0 if self.frames_sent % 40 == 39 else v3.TRACKED
        self._send(v3.FRAME, payload, schema_id=self.schema_id, sequence=self.sequence,
                   flags=flags, token=v3.source_token("hapihapi"))
        self.frames_sent += 1

    def start_push(self) -> None:
        """Simulate an explicit iOS manual Start: no HELLO/PC nonce needed."""
        if self.push_target is None:
            raise ValueError('push_target required')
        now = time.monotonic()
        self.peer = self.push_target
        self.session_id = secrets.randbits(64) or 1
        self.nonce = 0  # Contract-4 manual start marker (not authentication).
        self.schema_id, self.sequence, self.frames_sent = 1, 0, 0
        self.fps, self.udp_size = 60, self.max_udp_size
        self.changed = False
        self.schema_attempts = 0
        self.delivery = SchemaDelivery(self.transport)
        if self.transport == 'tcp':
            self.connection = socket.create_connection(
                self.push_target, timeout=2, source_address=(self.sock.getsockname()[0], 0))
            self.connection.settimeout(0.01)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._make_schema(now)
        self.last_control = self._next_frame = now
        self._send_schema(now)

    def serve(self, stop_event: threading.Event) -> None:
        LOG.info("Synthetic %s sender on %s:%d", self.transport, self.sock.getsockname()[0], self.port)
        try:
            if self.push_target is not None:
                if stop_event.wait(self.push_delay):
                    return
                self.start_push()
            while not stop_event.is_set():
                try:
                    if self.transport == "udp":
                        try:
                            data, peer = self.sock.recvfrom(65536)
                        except socket.timeout:
                            data = None
                        if data is not None:
                            self._control(v3.decode_packet(data, max_udp_size=v3.MAX_UDP_SIZE), peer)
                    else:
                        if self.connection is None:
                            try:
                                self.connection, _ = self.sock.accept()
                                self.connection.settimeout(0.01)
                                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                                self._framer = v3.TCPFramer()
                                self.session_id = 0
                            except socket.timeout:
                                continue
                        try:
                            data = self.connection.recv(65536)
                        except socket.timeout:
                            data = None
                        if data == b"":
                            self.connection.close()
                            self.connection = None
                            self.session_id = 0
                        elif data:
                            for packet in self._framer.feed(data):
                                self._control(packet)
                    self._tick()
                except (OSError, v3.ProtocolError) as exc:
                    if stop_event.is_set():
                        break
                    LOG.warning("simulator dropped connection/message: %s", exc)
                    if self.connection:
                        self.connection.close()
                        self.connection = None
                        self.session_id = 0
        finally:
            if self.connection:
                self.connection.close()
            self.sock.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=("udp", "tcp"), default="udp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="simulated iOS port; selected by app/transport if omitted")
    parser.add_argument("--app", choices=v3.APPS, default="iFacialMocap")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--change-after", type=int, default=30)
    parser.add_argument("--reverse-fragments", action="store_true")
    parser.add_argument("--drop-first-schema", action="store_true")
    parser.add_argument("--drop-first-schema-part", type=int)
    parser.add_argument("--max-udp-size", type=int, default=1200)
    parser.add_argument("--push-host", help="simulate manual iOS push to this PC")
    parser.add_argument("--push-port", type=int, default=None, help="PC port; default UDP 49983 / TCP 49986")
    parser.add_argument("--push-delay", type=float, default=0)
    parser.add_argument("--mute-frames", action="store_true", help="SCHEMA/PONG only; watchdog test")
    parser.add_argument("--mute-after", type=int, default=0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    server = Simulator(transport=args.transport, host=args.host, port=args.port, app=args.app,
                       count=args.count, change_after=args.change_after, reverse_fragments=args.reverse_fragments,
                       drop_first_schema=args.drop_first_schema,
                       drop_first_schema_part=args.drop_first_schema_part, max_udp_size=args.max_udp_size,
                       push_target=(args.push_host, args.push_port if args.push_port is not None else v3.default_ports(args.app, args.transport)[1]) if args.push_host else None,
                       push_delay=args.push_delay, mute_frames=args.mute_frames, mute_after=args.mute_after)
    try:
        server.serve(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
