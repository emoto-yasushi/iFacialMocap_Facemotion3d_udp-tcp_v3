#!/usr/bin/env python3
"""Synthetic iOS sender: test a Face Motion v3 receiver without an iPhone.

Keep face_motion_v3.py in the same folder (its codec is reused). Values and
names are synthetic, not ARKit data. This is a test tool, not the iOS app.

Normal start on one PC (two terminals; the simulated iOS needs its own port):
    1. python simulate_ios_v3.py --ios-port 51083
    2. python face_motion_v3.py --host 127.0.0.1 --ios-port 51083
Manual start (the simulated iOS sends first):
    1. python face_motion_v3.py --listen
    2. python simulate_ios_v3.py --push-host 127.0.0.1
Add --transport tcp to BOTH commands for TCP. Stop each with Ctrl+C.

By default 3 BlendShapes are sent and a 4th is added after 30 frames (a new
SCHEMA, as when playback of a recording with another BlendShape list starts);
--change-after 0 keeps one SCHEMA. --count sets the number of BlendShapes.
"""
from __future__ import annotations

import argparse
import errno
import logging
import math
import secrets
import socket
import threading
import time
from collections import deque
from typing import Sequence

import face_motion_v3 as v3

LOG = logging.getLogger("simulate_ios_v3")

# iOS-side timing (seconds). The receiver's timers are in face_motion_v3.RecoveryPolicy.
SCHEMA_RESEND_INTERVAL = 1.0    # UDP: resend an unacknowledged SCHEMA; also the limit for requested resends
SCHEMA_ACK_TIMEOUT = 10.0       # UDP: give up when the current SCHEMA is not acknowledged
HELLO_TIMEOUT = 5.0             # TCP: a complete HELLO must arrive this soon after accept
LEASE_MS = 10_000               # session ends without a valid control message from the PC

APP_NAME = "iFacialMocap"          # the "app" text in the simulated SCHEMA (receivers accept any)
EXAMPLE_SOURCE_TOKEN = 0x12345678  # the apps use their own value; receivers may ignore it


class SchemaDelivery:
    """When to (re)send SCHEMA and whether FRAMEs may be sent, as iOS decides it.

    UDP: FRAMEs wait for the SCHEMA_ACK of the current schema_id. A change during
    that wait replaces the pending schema without restarting the 10-second limit.
    TCP: the SCHEMA is written once before the FRAMEs that use it; no ACK exists.
    An acknowledged SCHEMA is sent again only on request (GET_SCHEMA or the same HELLO).
    """

    def __init__(self, transport: str) -> None:
        self.transport = transport
        self.schema_id = 0
        self.body = b""
        self.acknowledged_id = 0
        self.wait_started: float | None = None
        self.last_sent = -math.inf
        self.sent_current = False
        self.resend_requested = False
        self.expired = False

    def install(self, schema_id: int, body: bytes, now: float) -> None:
        if schema_id <= self.schema_id:
            raise ValueError("schema_id must increase")
        self.schema_id, self.body, self.sent_current = schema_id, body, False
        self.acknowledged_id = 0
        if self.transport == "udp" and self.wait_started is None:
            self.wait_started = now          # a later change does not extend this wait

    def check_timeout(self, now: float) -> bool:
        if (self.transport == "udp" and self.wait_started is not None
                and now - self.wait_started >= SCHEMA_ACK_TIMEOUT):
            self.expired = True
        return self.expired

    def due(self, now: float, *, requested: bool = False) -> bool:
        """True when the complete SCHEMA should be sent now (UDP: at most once per second).

        A request is remembered until the SCHEMA is sent, so a request within the
        one-second limit is served a little later instead of being lost.
        """
        if not self.schema_id or self.check_timeout(now):
            return False
        self.resend_requested = self.resend_requested or requested
        if self.transport == "tcp":
            return not self.sent_current or self.resend_requested
        if self.can_send_frames() and not self.resend_requested:
            return False
        return now - self.last_sent >= SCHEMA_RESEND_INTERVAL

    def mark_sent(self, now: float) -> None:
        self.last_sent, self.sent_current, self.resend_requested = now, True, False
        if self.transport == "tcp":
            self.acknowledged_id = self.schema_id

    def acknowledge(self, schema_id: int, now: float) -> bool:
        """Accept only an ACK for the current, already sent schema before the deadline."""
        if self.transport != "udp" or self.check_timeout(now) or not self.sent_current:
            return False
        if schema_id != self.schema_id:
            return False
        self.acknowledged_id, self.wait_started, self.resend_requested = schema_id, None, False
        return True

    def can_send_frames(self) -> bool:
        return not self.expired and self.schema_id != 0 and self.acknowledged_id == self.schema_id


class Simulator:
    """One simulated iOS app. Normal start: wait for HELLO. Manual start: push_to=(PC host, port)."""

    def __init__(self, *, transport: str = "udp", host: str = "127.0.0.1",
                 ios_port: int | None = None, push_to: tuple[str, int] | None = None,
                 count: int = 3, change_after: int = 30, max_udp_size: int = v3.MAX_UDP_SIZE,
                 reverse_fragments: bool = False, drop_first_schema: bool = False,
                 drop_first_schema_part: int | None = None, mute_frames: bool = False,
                 mute_after: int = 0) -> None:
        if transport not in ("udp", "tcp") or not 3 <= count <= 4095:
            raise ValueError("invalid simulator configuration")
        if not v3.MIN_UDP_SIZE <= max_udp_size <= v3.MAX_UDP_SIZE:
            raise ValueError("max_udp_size must be 576-1200")
        self.transport, self.app_name = transport, APP_NAME
        # Replies arrive from an IP address, so resolve a host name once here.
        self.push_to = None if push_to is None else (socket.gethostbyname(push_to[0]), push_to[1])
        self.count, self.change_after, self.max_udp_size = count, change_after, max_udp_size
        self.reverse_fragments = reverse_fragments
        self.drop_first_schema, self.drop_first_schema_part = drop_first_schema, drop_first_schema_part
        self.mute_frames, self.mute_after = mute_frames, mute_after
        # A manual start sends from any free port; a normal start listens on the iOS port.
        if push_to is not None:
            port = 0
        else:
            port = v3.default_ports(transport)[0] if ios_port is None else ios_port
        self.sock: socket.socket | None = None
        if transport == "udp" or push_to is None:
            self.sock = self._bind(host, port)
        self.port = self.sock.getsockname()[1] if self.sock else None
        self.connection: socket.socket | None = None
        self.accepted_at = 0.0
        self.framer = v3.TCPFramer()
        self.controls: deque[int] = deque(maxlen=4096)  # message types received (for tests)
        self.sent: deque[int] = deque(maxlen=4096)      # message types sent (for tests)
        self._end_session()

    def _bind(self, host: str, port: int) -> socket.socket:
        # No SO_REUSEADDR: two programs must never share a port without an error.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if self.transport == "udp" else socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in (10048, 10013):
                raise v3.PortInUseError(
                    exc.errno, f"port {port} is already in use. When the receiver runs on this PC, "
                    "give the simulated iOS its own port, e.g. --ios-port 51083.") from exc
            raise
        sock.settimeout(0.02)
        if self.transport == "tcp":
            sock.listen(1)
        return sock

    # ---- session -----------------------------------------------------------------

    def _end_session(self) -> None:
        self.session_id = 0
        self.peer = None
        self.nonce = 0
        self.hello_payload = b""
        self.fps, self.udp_size = 60, self.max_udp_size
        self.schema_id, self.sequence, self.frames_sent = 0, 0, 0
        self.changed = False
        self.schema_sends = 0
        self.delivery = SchemaDelivery(self.transport)
        self.last_control = self.next_frame = 0.0

    def _begin_session(self, now: float, *, nonce: int, peer, fps: int, udp_size: int) -> None:
        self._end_session()
        self.session_id = secrets.randbits(64) or 1
        self.nonce, self.peer = nonce, peer
        self.fps, self.udp_size = fps, udp_size
        self.last_control = self.next_frame = now
        self._install_schema(now)
        self._send_schema(now)                   # SCHEMA is the only positive answer to HELLO

    def _names(self) -> list[str]:
        names = ["jawOpen", "eyeBlinkLeft", "myCustomSmile"] + [f"custom_{i:04d}" for i in range(self.count - 3)]
        return names + ["myCustomAngry"] if self.changed else names

    def _install_schema(self, now: float) -> None:
        self.schema_id += 1
        json_bytes = v3.schema_json(self._names(), app=self.app_name)
        self.schema = v3.Schema.parse(json_bytes, self.session_id, self.schema_id)
        start = v3.StartInfo(self.nonce, self.fps, self.udp_size, LEASE_MS)
        self.delivery.install(self.schema_id, v3.schema_body(json_bytes, start), now)

    # ---- sending -----------------------------------------------------------------

    def _write(self, raws: list[bytes], *, peer=None) -> None:
        for raw in raws:
            self.sent.append(raw[4])
            if self.transport == "udp":
                target = peer or self.peer
                if target is not None:
                    self.sock.sendto(raw, target)
            elif self.connection is not None:
                self.connection.sendall(raw)

    def _send(self, kind: int, payload: bytes = b"", **header) -> None:
        raws = v3.encode_message(kind, payload, session_id=self.session_id,
                                 udp_size=self.udp_size if self.transport == "udp" else None, **header)
        if self.reverse_fragments and self.transport == "udp":
            raws.reverse()
        self._write(raws)

    def _send_schema(self, now: float) -> None:
        self.schema_sends += 1
        raws = v3.encode_message(v3.SCHEMA, self.delivery.body, session_id=self.session_id,
                                 schema_id=self.schema_id,
                                 udp_size=self.udp_size if self.transport == "udp" else None)
        if self.reverse_fragments and self.transport == "udp":
            raws.reverse()
        if self.schema_sends == 1:               # test options: lose parts of the first SCHEMA
            if self.drop_first_schema:
                raws = []
            elif self.drop_first_schema_part is not None:
                raws = [r for r in raws if v3.decode_packet(r).part_index != self.drop_first_schema_part]
        self._write(raws)
        self.delivery.mark_sent(now)

    def _refuse(self, nonce: int, reason: str, peer=None) -> None:
        """ERROR to a HELLO that does not start a session: header carries the HELLO nonce."""
        self._write(v3.encode_message(v3.ERROR, reason.encode(), session_id=nonce), peer=peer)

    # ---- receiving ---------------------------------------------------------------

    def _on_hello(self, packet: v3.Packet, peer, now: float) -> None:
        fps, udp_size, revision = v3.HELLO_BODY.unpack(packet.payload)
        if revision != v3.CONTRACT_REVISION:
            self._refuse(packet.session_id, f"UNSUPPORTED_CONTRACT: requires {v3.CONTRACT_REVISION}", peer)
        elif not 1 <= fps <= 60 or not v3.MIN_UDP_SIZE <= udp_size <= v3.MAX_UDP_SIZE:
            self._refuse(packet.session_id, "INVALID_HELLO", peer)
        elif not self.session_id:
            self._begin_session(now, nonce=packet.session_id, peer=peer, fps=fps,
                                udp_size=min(udp_size, self.max_udp_size))
            self.hello_payload = packet.payload
        elif (peer is not None and peer != self.peer) or packet.session_id != self.nonce:
            self._refuse(packet.session_id, "BUSY", peer)
        elif packet.payload != self.hello_payload:
            self._refuse(packet.session_id, "HELLO_SETTINGS_CHANGED", peer)
        else:                                    # the same HELLO again: resend, never reset
            self.last_control = now
            if self.delivery.due(now, requested=True):
                self._send_schema(now)

    def _on_control(self, packet: v3.Packet, peer, now: float) -> None:
        self.controls.append(packet.kind)
        if packet.kind == v3.HELLO:
            self._on_hello(packet, peer, now)
            return
        if not self.session_id or packet.session_id != self.session_id or (peer is not None and peer != self.peer):
            return
        if packet.kind == v3.SCHEMA_ACK:
            if self.delivery.acknowledge(packet.schema_id, now):
                self.last_control = now          # as in the apps: a stale or wrong ACK does not extend the lease
        elif packet.kind == v3.GET_SCHEMA:
            self.last_control = now
            if self.delivery.due(now, requested=True):
                self._send_schema(now)
        elif packet.kind == v3.PING:
            self.last_control = now
            self._send(v3.PONG, sequence=packet.sequence)
        elif packet.kind == v3.STOP:
            self._end_session()
        else:
            raise v3.ProtocolError(f"{v3.TYPE_NAMES[packet.kind]} is never sent by a receiver")

    # ---- timers ------------------------------------------------------------------

    def _tick(self, now: float) -> None:
        if not self.session_id:
            return
        if self.delivery.check_timeout(now):
            self._send(v3.ERROR, b"SCHEMA_ACK_TIMEOUT")
            self._end_session()
            return
        if now - self.last_control >= LEASE_MS / 1000:
            self._end_session()
            return
        if self.change_after > 0 and not self.changed and self.frames_sent >= self.change_after:
            self.changed = True                  # e.g. playback of a recording with another BlendShape list starts
            self._install_schema(now)
        if self.delivery.due(now):
            self._send_schema(now)
        if not self.delivery.can_send_frames() or now < self.next_frame:
            return
        if self.mute_frames or (self.mute_after and self.frames_sent >= self.mute_after):
            return
        self.next_frame = now + 1.0 / self.fps
        values = [self.frames_sent % 101, 0, -25] + [0] * (len(self.schema.blend_names) - 3)
        if self.changed:
            values[-1] = 150                     # values above 100 are valid
        pose = [1.25, -2.5, 3.75, 0.1, -0.2, 0.3, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        flags = 0 if self.frames_sent % 40 == 39 else v3.TRACKED   # shows tracking loss now and then
        self._send(v3.FRAME, v3.frame_payload(self.schema, values, pose), schema_id=self.schema_id,
                   sequence=self.sequence, flags=flags, token=EXAMPLE_SOURCE_TOKEN)
        self.frames_sent += 1

    # ---- loops -------------------------------------------------------------------

    def _start_push(self, now: float) -> None:
        """Manual start: iOS sends SCHEMA (client_nonce 0) to the PC without a HELLO."""
        if self.transport == "tcp":
            self.connection = socket.create_connection(self.push_to, timeout=2)
            self.connection.settimeout(0.02)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.framer = v3.TCPFramer()
        self._begin_session(now, nonce=0, peer=self.push_to, fps=60, udp_size=self.max_udp_size)

    def _close_connection(self) -> None:
        if self.connection is not None:
            self.connection.close()
        self.connection = None
        self._end_session()

    def _receive_tcp(self, now: float) -> None:
        if self.connection is None:
            if self.push_to is not None:
                return
            try:
                self.connection, _ = self.sock.accept()
            except socket.timeout:
                return
            self.connection.settimeout(0.02)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.framer, self.accepted_at = v3.TCPFramer(), now
            self._end_session()
        if not self.session_id and self.push_to is None and now - self.accepted_at >= HELLO_TIMEOUT:
            LOG.info("No complete HELLO within %.0f s; closing the connection", HELLO_TIMEOUT)
            self._close_connection()
            return
        try:
            data = self.connection.recv(65536)
        except socket.timeout:
            return
        if not data:
            self._close_connection()
            return
        for packet in self.framer.feed(data):
            self._on_control(packet, None, now)

    def serve(self, stop_event: threading.Event, *, push_delay: float = 0.0) -> None:
        where = f"port {self.port}" if self.port else "a free port"
        LOG.info("Simulated %s iOS (%s) on %s", self.transport.upper(), self.app_name, where)
        try:
            if self.push_to is not None:
                if stop_event.wait(push_delay):
                    return
                self._start_push(time.monotonic())
            while not stop_event.is_set():
                now = time.monotonic()
                # Wake up in time for the next FRAME, but at least every 20 ms.
                wait = min(0.02, max(0.001, self.next_frame - now)) if self.session_id else 0.02
                for sock in (self.sock, self.connection):
                    if sock is not None:
                        sock.settimeout(wait)
                try:
                    if self.transport == "udp":
                        try:
                            data, peer = self.sock.recvfrom(65536)
                            self._on_control(v3.decode_packet(data, max_udp_size=v3.MAX_UDP_SIZE), peer, now)
                        except socket.timeout:
                            pass
                    else:
                        self._receive_tcp(now)
                    self._tick(time.monotonic())
                except (OSError, v3.ProtocolError) as exc:
                    if stop_event.is_set():
                        break
                    LOG.warning("Dropped message/connection: %s", exc)
                    if self.transport == "tcp":
                        self._close_connection()
        finally:
            if self.connection is not None:
                self.connection.close()
            if self.sock is not None:
                self.sock.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=("udp", "tcp"), default="udp")
    parser.add_argument("--host", default="127.0.0.1", help="address the simulated iOS listens on")
    parser.add_argument("--ios-port", type=int, help="port the simulated iOS listens on (default UDP 49983 / TCP 49984)")
    parser.add_argument("--push-host", help="manual start: send to the receiver (PC) at this address")
    parser.add_argument("--pc-port", type=int, help="manual start: receiver (PC) port (default UDP 49983 / TCP 49984)")
    parser.add_argument("--push-delay", type=float, default=0.0, help="manual start: wait this many seconds first")
    parser.add_argument("--count", type=int, default=3, help="number of BlendShapes (3-4095)")
    parser.add_argument("--change-after", type=int, default=30,
                        help="add one BlendShape (a new SCHEMA) after N frames; 0 = never")
    parser.add_argument("--max-udp-size", type=int, default=v3.MAX_UDP_SIZE)
    tests = parser.add_argument_group("fault injection for receiver tests")
    tests.add_argument("--reverse-fragments", action="store_true", help="send UDP fragments in reverse order")
    tests.add_argument("--drop-first-schema", action="store_true", help="lose the whole first SCHEMA")
    tests.add_argument("--drop-first-schema-part", type=int, help="lose one fragment of the first SCHEMA")
    tests.add_argument("--mute-frames", action="store_true", help="never send FRAME (SCHEMA/PONG only)")
    tests.add_argument("--mute-after", type=int, default=0, help="stop sending FRAME after N frames")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    push_to = None
    if args.push_host:
        push_to = (args.push_host, args.pc_port or v3.default_ports(args.transport)[1])
    try:
        simulator = Simulator(transport=args.transport, host=args.host, ios_port=args.ios_port,
                              push_to=push_to, count=args.count, change_after=args.change_after,
                              max_udp_size=args.max_udp_size, reverse_fragments=args.reverse_fragments,
                              drop_first_schema=args.drop_first_schema,
                              drop_first_schema_part=args.drop_first_schema_part,
                              mute_frames=args.mute_frames, mute_after=args.mute_after)
        simulator.serve(threading.Event(), push_delay=args.push_delay)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
