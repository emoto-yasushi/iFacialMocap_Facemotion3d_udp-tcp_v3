#!/usr/bin/env python3
"""iFacialMocap / Facemotion3d v3 client and binary codec (Python 3.10+).

Contract revision 4:
UDP: HELLO -> SCHEMA -> SCHEMA_ACK -> FRAME (ACK required for each new table).
TCP: HELLO -> SCHEMA -> FRAME (ordered stream; no application ACK required).
No WELCOME or READY message. Not a v1/v2 or contract-1/2/3 receiver.
Requires an iOS build supporting standard-port FMV3 contract_revision=4.
No third-party packages, Bluetooth, eval, pickle, compression, or legacy parsing.

CLI: python face_motion_v3.py --host 192.168.1.20 --transport udp
Manual push: python face_motion_v3.py --listen --transport udp
Facemotion3d: add --app facemotion3d (iOS UDP 49993 / TCP 49994).
iFacialMocap defaults: iOS UDP 49983 / TCP 49984; PC UDP 49983 / TCP 49986.
Port selection does not change contract_revision=4 or the binary wire format.
API: V3Client(host, transport="tcp").run(on_frame, on_schema=on_schema)
All networking runs in the calling thread. Callbacks should return promptly.
Console: all BlendShapes, head/eyes and frame metadata on one line per update.
The default --log-every 1.0 limits DISPLAY only; every valid frame is still
received and dispatched. A frame log ends in one newline, with no blank line.
"""
from __future__ import annotations

import argparse
import errno
import select
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import logging
import math
import secrets
import socket
import struct
import threading
import time
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

LOG = logging.getLogger("face_motion_v3")
MAGIC = b"FMV3"
# Compatibility aliases mean the iFacialMocap UDP defaults only.
# V3Client resolves defaults per transport/app; explicit port overrides win.
DEFAULT_PORT = 49983
DEFAULT_LISTEN_PORT = 49983
PORT_PROFILES = MappingProxyType({
    "ifacialmocap": MappingProxyType({"udp": (49983, 49983), "tcp": (49984, 49986)}),
    "facemotion3d": MappingProxyType({"udp": (49993, 49983), "tcp": (49994, 49986)}),
})


def default_ports(app: str = "ifacialmocap", transport: str = "udp") -> tuple[int, int]:
    """Return (iOS active-start port, PC receive/listen port).

    TCP uses the existing iOS direct-TCP listener, not the legacy UDP-triggered
    reverse connection. PC manual TCP listening uses the legacy 49986 endpoint.
    Facemotion3d PC UDP 49983 follows the Other output setting/sample; its native
    discovery fallback 49993 remains configurable via listen_port.
    """
    if not isinstance(app, str) or app.lower() not in PORT_PROFILES:
        raise ValueError("app must be ifacialmocap or facemotion3d")
    if transport not in ("udp", "tcp"):
        raise ValueError("transport must be udp or tcp")
    return PORT_PROFILES[app.lower()][transport]

HEADER = struct.Struct("<4sBBHQIIIIHHI")
HEADER_SIZE = HEADER.size  # 40, no native padding
HELLO_BODY = struct.Struct("<HHI")
CONTRACT_REVISION = 4
SCHEMA_INFO = struct.Struct("<QHHII")  # nonce, fps, frame UDP limit, lease ms, revision
SCHEMA_UDP_SIZE = 576  # bootstrap is independent of the negotiated FRAME limit
POSE = struct.Struct("<12f")
MIN_UDP_SIZE, MAX_UDP_SIZE = 576, 1200
MAX_SCHEMA_BYTES = 262144  # entire SCHEMA body, including its 20-byte start info
MAX_SCHEMA_JSON_BYTES = MAX_SCHEMA_BYTES - SCHEMA_INFO.size
MAX_BLENDSHAPES = 4096
MAX_FRAME_BYTES = MAX_BLENDSHAPES * 4 + POSE.size
MAX_FRAGMENTS = 512
MAX_REASSEMBLY_BYTES = 524288
MAX_REASSEMBLY_GROUPS = 8
MAX_NAME_BYTES = 255
POSE_LAYOUT = "head_rxyz_pxyz_rightEye_rxyz_leftEye_rxyz"
HELLO, SCHEMA, FRAME, GET_SCHEMA, PING, PONG, STOP, ERROR, SCHEMA_ACK = (1, 3, 4, 5, 6, 7, 8, 9, 10)
# Type 2 is reserved. Do not reuse it.
TYPE_NAMES = {1: "HELLO", 3: "SCHEMA", 4: "FRAME", 5: "GET_SCHEMA",
              6: "PING", 7: "PONG", 8: "STOP", 9: "ERROR", 10: "SCHEMA_ACK"}
TRACKED, PLAYBACK = 1, 2
APPS = ("iFacialMocap", "Facemotion3d")
PROFILES = {"iFacialMocap": "ifacialmocap-stream", "Facemotion3d": "facemotion3d-other"}
CONTROL_SIZES = {HELLO: 8, GET_SCHEMA: 0, PING: 0, PONG: 0,
                 STOP: 0, SCHEMA_ACK: 0}


class ProtocolError(ValueError):
    """Malformed or inconsistent v3 data. Never pass the affected frame onward."""


class RemoteError(RuntimeError):
    """The selected iOS endpoint explicitly declined the v3 request."""


class CallbackError(RuntimeError):
    """Consumer callback failed; never treat it as a network reconnection request."""


def _invoke_callback(callback: Callable, argument: object) -> None:
    try:
        callback(argument)
    except Exception as exc:
        raise CallbackError(f"{type(argument).__name__} callback failed: {exc}") from exc


def _uint(value: int, bits: int, label: str) -> int:
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ProtocolError(f"{label} must be an unsigned {bits}-bit integer")
    return value


def is_newer_u32(candidate: int, previous: int) -> bool:
    """Modulo comparison; half-range (2**31) is intentionally ambiguous."""
    delta = (candidate - previous) & 0xFFFFFFFF
    return 0 < delta < 0x80000000


def source_token(value: str | None) -> int:
    """FNV-1a-32 of UTF-8; only ASCII SP/TAB/CR/LF are trimmed.

    Empty / literal lowercase 'none' => 0. Hash result 0 => 1.
    A public identifier, NOT authentication. Do not use Python/Swift hash().
    """
    text = (value or "").strip(" \t\r\n")
    if not text or text == "none":
        return 0
    h = 2166136261
    for byte in text.encode("utf-8"):
        h = ((h ^ byte) * 16777619) & 0xFFFFFFFF
    return h or 1


@dataclass(frozen=True)
class Packet:
    kind: int
    flags: int
    session_id: int
    schema_id: int
    sequence: int
    source_token: int
    total_length: int
    part_index: int
    part_count: int
    payload: bytes


def _read_header(data: bytes | bytearray | memoryview) -> tuple:
    if len(data) < HEADER_SIZE:
        raise ProtocolError("incomplete v3 header")
    magic, kind, flags, size, session, schema, seq, token, total, index, count, length = HEADER.unpack_from(data)
    if magic != MAGIC or size != HEADER_SIZE:
        raise ProtocolError("wrong magic/header size (this client requires v3)")
    if kind not in TYPE_NAMES:
        raise ProtocolError("unknown message type")
    if flags & ~(TRACKED | PLAYBACK) or (kind != FRAME and flags):
        raise ProtocolError("invalid flags")
    if session == 0:
        raise ProtocolError("zero session/HELLO nonce")
    if count < 1 or count > MAX_FRAGMENTS or index >= count:
        raise ProtocolError("invalid fragment index/count")
    if kind in (SCHEMA, FRAME, SCHEMA_ACK) and schema == 0:
        raise ProtocolError("this message requires a nonzero schema id")
    if kind in (HELLO, PING, PONG, STOP, ERROR) and schema != 0:
        raise ProtocolError("unexpected schema id")
    if kind != FRAME and token != 0:
        raise ProtocolError("source token is only defined on FRAME")
    if kind in (HELLO, SCHEMA, GET_SCHEMA, STOP, SCHEMA_ACK) and seq != 0:
        raise ProtocolError("unexpected sequence field")
    limit = MAX_SCHEMA_BYTES if kind == SCHEMA else MAX_FRAME_BYTES if kind == FRAME else 512
    if total > limit or length > total:
        raise ProtocolError("payload exceeds protocol limit")
    if kind in CONTROL_SIZES and total != CONTROL_SIZES[kind]:
        raise ProtocolError("incorrect control-message size")
    if kind in (SCHEMA, ERROR) and total == 0:
        raise ProtocolError("empty schema/error")
    if count == 1:
        if index != 0 or length != total:
            raise ProtocolError("invalid single-part length")
    elif kind not in (SCHEMA, FRAME) or length == 0 or count > total:
        raise ProtocolError("invalid fragmented message")
    return kind, flags, session, schema, seq, token, total, index, count, length


def decode_packet(data: bytes, *, max_udp_size: int | None = None) -> Packet:
    if max_udp_size is not None and len(data) > max_udp_size:
        raise ProtocolError("UDP datagram exceeds negotiated size")
    values = _read_header(data)
    *fields, length = values
    if len(data) != HEADER_SIZE + length:
        raise ProtocolError("packet length mismatch")
    if max_udp_size is not None:
        total, index, count = values[6:9]
        wire_limit = SCHEMA_UDP_SIZE if values[0] == SCHEMA else max_udp_size
        if len(data) > wire_limit:
            raise ProtocolError("SCHEMA datagram exceeds bootstrap limit")
        capacity = wire_limit - HEADER_SIZE
        expected_count = max(1, (total + capacity - 1) // capacity)
        expected_length = min(capacity, total - index * capacity)
        if count != expected_count or length != expected_length:
            raise ProtocolError("noncanonical UDP fragmentation")
    return Packet(*fields, bytes(data[HEADER_SIZE:]))


def encode_message(kind: int, payload: bytes = b"", *, session_id: int,
                   schema_id: int = 0, sequence: int = 0, token: int = 0,
                   flags: int = 0, udp_size: int | None = None) -> list[bytes]:
    """Return one TCP message, or canonical UDP fragments. No TCP extra prefix."""
    for value, bits, name in ((session_id, 64, "session"), (schema_id, 32, "schema"),
                              (sequence, 32, "sequence"), (token, 32, "token")):
        _uint(value, bits, name)
    if type(payload) is not bytes:
        raise TypeError("payload must be bytes")
    if udp_size is not None and not MIN_UDP_SIZE <= udp_size <= MAX_UDP_SIZE:
        raise ProtocolError("UDP size must be 576..1200")
    wire_limit = SCHEMA_UDP_SIZE if kind == SCHEMA and udp_size is not None else udp_size
    capacity = (wire_limit - HEADER_SIZE) if wire_limit is not None else max(1, len(payload))
    count = max(1, (len(payload) + capacity - 1) // capacity)
    result = []
    for index in range(count):
        piece = payload[index * capacity:(index + 1) * capacity]
        head = HEADER.pack(MAGIC, kind, flags, HEADER_SIZE, session_id, schema_id,
                           sequence, token, len(payload), index, count, len(piece))
        raw = head + piece
        decode_packet(raw, max_udp_size=udp_size)  # also validate encoders used by tests/simulator
        result.append(raw)
    return result


class TCPFramer:
    """Incremental stream framing. Handles partial headers/payloads and coalescing."""
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        if len(data) > 65536:
            raise ProtocolError("feed TCPFramer in chunks of at most 65536 bytes")
        self._buffer.extend(data)
        packets = []
        offset = 0
        while len(self._buffer) - offset >= HEADER_SIZE:
            values = _read_header(memoryview(self._buffer)[offset:offset + HEADER_SIZE])
            if values[8] != 1:
                raise ProtocolError("TCP does not use application fragmentation")
            size = HEADER_SIZE + values[9]
            if len(self._buffer) - offset < size:
                break
            packets.append(decode_packet(bytes(self._buffer[offset:offset + size])))
            offset += size
        if offset:
            del self._buffer[:offset]
        if len(self._buffer) > HEADER_SIZE + MAX_SCHEMA_BYTES:
            raise ProtocolError("TCP receive buffer exceeded limit")
        return packets

    def eof(self) -> None:
        if self._buffer:
            raise ProtocolError("TCP closed with an incomplete message")


@dataclass
class _Assembly:
    packet: Packet
    created: float
    parts: dict[int, bytes] = field(default_factory=dict)
    size: int = 0


class Reassembler:
    """Bounded reassembly. Dropped frames are never retransmitted or extrapolated."""
    def __init__(self) -> None:
        self._groups: OrderedDict[tuple, _Assembly] = OrderedDict()
        self._bytes = 0

    def _remove(self, key: tuple) -> None:
        group = self._groups.pop(key, None)
        if group is not None:
            self._bytes -= group.size

    def expire(self, now: float) -> None:
        for key, group in list(self._groups.items()):
            timeout = 3.0 if group.packet.kind == SCHEMA else 0.25
            if now - group.created >= timeout:
                self._remove(key)

    def clear(self) -> None:
        self._groups.clear()
        self._bytes = 0

    def push(self, packet: Packet, now: float) -> bytes | None:
        self.expire(now)
        if packet.part_count == 1:
            return packet.payload
        key = (packet.session_id, packet.kind, packet.schema_id, packet.sequence)
        group = self._groups.get(key)
        if group is None:
            while len(self._groups) >= MAX_REASSEMBLY_GROUPS:
                self._remove(next(iter(self._groups)))
            group = _Assembly(packet, now)
            self._groups[key] = group
        a = group.packet
        if (a.flags, a.source_token, a.total_length, a.part_count) != (
                packet.flags, packet.source_token, packet.total_length, packet.part_count):
            self._remove(key)
            raise ProtocolError("conflicting fragment metadata")
        previous = group.parts.get(packet.part_index)
        if previous is not None:
            if previous != packet.payload:
                self._remove(key)
                raise ProtocolError("conflicting duplicate fragment")
            return None
        if group.size + len(packet.payload) > packet.total_length:
            self._remove(key)
            raise ProtocolError("fragment bytes exceed total")
        while self._bytes + len(packet.payload) > MAX_REASSEMBLY_BYTES:
            other = next((k for k in self._groups if k != key), None)
            if other is None:
                self._remove(key)
                raise ProtocolError("reassembly memory limit")
            self._remove(other)
        group.parts[packet.part_index] = packet.payload
        group.size += len(packet.payload)
        self._bytes += len(packet.payload)
        if len(group.parts) != packet.part_count:
            return None
        result = b"".join(group.parts[i] for i in range(packet.part_count))
        self._remove(key)
        if len(result) != packet.total_length:
            raise ProtocolError("reassembled length mismatch")
        return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> None:
    raise ProtocolError(f"nonstandard JSON constant {value}")


@dataclass(frozen=True)
class Schema:
    session_id: int
    schema_id: int
    app: str
    profile: str
    blend_names: tuple[str, ...]
    blend_encoding: str
    index_by_name: Mapping[str, int]
    blend_struct: struct.Struct
    wire_bytes: bytes

    @property
    def frame_bytes(self) -> int:
        return self.blend_struct.size + POSE.size

    @classmethod
    def parse(cls, payload: bytes, session_id: int, schema_id: int) -> Schema:
        if not payload or len(payload) > MAX_SCHEMA_JSON_BYTES:
            raise ProtocolError("schema size out of range")
        try:
            obj = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_json_constant)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ProtocolError(f"invalid schema JSON: {exc}") from exc
        if type(obj) is not dict:
            raise ProtocolError("schema must be a JSON object")
        expected = {"schema_version", "app", "profile", "blend_names", "blend_encoding",
                    "blend_unit", "pose_layout", "rotation_unit", "position_unit"}
        if set(obj) != expected:
            raise ProtocolError("unsupported or missing schema keys")
        if type(obj["schema_version"]) is not int or obj["schema_version"] != 1:
            raise ProtocolError("unsupported schema version")
        app = obj["app"]
        if type(app) is not str or app not in APPS or obj["profile"] != PROFILES[app]:
            raise ProtocolError("unsupported application/profile")
        if (obj["blend_unit"] != "percent" or obj["pose_layout"] != POSE_LAYOUT
                or obj["rotation_unit"] != "degree" or obj["position_unit"] != "meter"):
            raise ProtocolError("unsupported numeric layout/units")
        encoding = obj["blend_encoding"]
        if encoding not in ("i16", "i32"):
            raise ProtocolError("unsupported blend encoding")
        names = obj["blend_names"]
        if type(names) is not list or len(names) > MAX_BLENDSHAPES:
            raise ProtocolError("invalid blend name list")
        seen = set()
        for name in names:
            if type(name) is not str:
                raise ProtocolError("blend name is not a string")
            try:
                encoded = name.encode("utf-8")
            except UnicodeError as exc:
                raise ProtocolError("invalid Unicode blend name") from exc
            if not 1 <= len(encoded) <= MAX_NAME_BYTES or any(ord(c) < 32 for c in name):
                raise ProtocolError("blend name empty, too long, or contains a control character")
            if name in seen:
                raise ProtocolError("duplicate blend name")
            seen.add(name)
        fmt = struct.Struct("<" + str(len(names)) + ("h" if encoding == "i16" else "i"))
        return cls(session_id, schema_id, app, obj["profile"], tuple(names), encoding,
                   MappingProxyType({name: i for i, name in enumerate(names)}), fmt, payload)


def schema_payload(names: Sequence[str], *, app: str = "iFacialMocap",
                   encoding: str = "i16") -> bytes:
    """Test/simulator helper; the iOS implementation must emit the same layout."""
    if app not in APPS:
        raise ProtocolError("unsupported app")
    obj = {"schema_version": 1, "app": app, "profile": PROFILES[app],
           "blend_names": list(names), "blend_encoding": encoding, "blend_unit": "percent",
           "pose_layout": POSE_LAYOUT, "rotation_unit": "degree", "position_unit": "meter"}
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    Schema.parse(data, 1, 1)
    return data


def hello_payload(fps: int = 60, udp_size: int = MAX_UDP_SIZE) -> bytes:
    """A single start request; its last UInt32 explicitly selects contract 4."""
    if type(fps) is not int or not 1 <= fps <= 60:
        raise ProtocolError("requested fps must be 1..60")
    if type(udp_size) is not int or not MIN_UDP_SIZE <= udp_size <= MAX_UDP_SIZE:
        raise ProtocolError("requested UDP size must be 576..1200")
    return HELLO_BODY.pack(fps, udp_size, CONTRACT_REVISION)


@dataclass(frozen=True)
class StartInfo:
    client_nonce: int
    actual_fps: int
    max_udp_size: int
    lease_ms: int

    @classmethod
    def parse(cls, payload: bytes) -> StartInfo:
        if len(payload) < SCHEMA_INFO.size:
            raise ProtocolError("incomplete SCHEMA start information")
        nonce, fps, size, lease, revision = SCHEMA_INFO.unpack_from(payload)
        if revision != CONTRACT_REVISION:
            raise ProtocolError("incompatible contract revision (requires revision 4)")
        if not 1 <= fps <= 60 or not MIN_UDP_SIZE <= size <= MAX_UDP_SIZE:
            raise ProtocolError("invalid SCHEMA start information")
        if not 5000 <= lease <= 60000:
            raise ProtocolError("invalid SCHEMA lease")
        return cls(nonce, fps, size, lease)

    def pack(self) -> bytes:
        _uint(self.client_nonce, 64, "client nonce")
        _uint(self.actual_fps, 16, "actual fps")
        _uint(self.max_udp_size, 16, "max UDP size")
        _uint(self.lease_ms, 32, "lease ms")
        raw = SCHEMA_INFO.pack(self.client_nonce, self.actual_fps,
                               self.max_udp_size, self.lease_ms, CONTRACT_REVISION)
        StartInfo.parse(raw)
        return raw


def schema_message_payload(json_payload: bytes, *, client_nonce: int,
                           actual_fps: int = 60, max_udp_size: int = MAX_UDP_SIZE,
                           lease_ms: int = 10000) -> bytes:
    """SCHEMA body = fixed 20-byte start info + uncompressed UTF-8 JSON table.

    Use for initial response, changes and retries. Cache the entire resulting body.
    schema_payload() returns ONLY the JSON table, for use with Schema.parse().
    """
    Schema.parse(json_payload, 1, 1)
    return StartInfo(client_nonce, actual_fps, max_udp_size, lease_ms).pack() + json_payload


@dataclass(frozen=True)
class Frame:
    schema: Schema
    sequence: int
    source_token: int
    tracking: bool
    playback: bool
    blend_values: tuple[int, ...]  # integer percent, -25 => normalized -0.25
    pose: tuple[float, ...]  # head rx/ry/rz/px/py/pz, right eye xyz, left eye xyz
    received_monotonic: float

    @property
    def head(self) -> tuple[float, ...]:
        return self.pose[:6]

    @property
    def right_eye(self) -> tuple[float, ...]:
        return self.pose[6:9]

    @property
    def left_eye(self) -> tuple[float, ...]:
        return self.pose[9:12]

    def blend(self, name: str, *, normalized: bool = False) -> int | float:
        value = self.blend_values[self.schema.index_by_name[name]]
        return value / 100.0 if normalized else value

    def to_dict(self) -> dict:
        """Optional display/export helper. Not called by the hot-path decoder."""
        return {"app": self.schema.app, "session_id": self.schema.session_id,
                "schema_id": self.schema.schema_id, "sequence": self.sequence,
                "source_token": self.source_token, "tracking": self.tracking,
                "playback": self.playback,
                "blend_shapes": dict(zip(self.schema.blend_names, self.blend_values)),
                "head": self.head, "right_eye": self.right_eye, "left_eye": self.left_eye}


def frame_payload(schema: Schema, values: Sequence[int], pose: Sequence[float]) -> bytes:
    if len(values) != len(schema.blend_names) or len(pose) != 12:
        raise ProtocolError("frame value count mismatch")
    if any(type(v) is not int for v in values):
        raise ProtocolError("blend values must be integer percent; no implicit truncation")
    if not all(math.isfinite(v) for v in pose):
        raise ProtocolError("nonfinite pose")
    try:
        result = schema.blend_struct.pack(*values) + POSE.pack(*pose)
    except (struct.error, OverflowError) as exc:
        raise ProtocolError(f"numeric value cannot be represented: {exc}") from exc
    # Float32 overflow may become Inf depending on the Python implementation.
    if not all(math.isfinite(v) for v in POSE.unpack_from(result, schema.blend_struct.size)):
        raise ProtocolError("pose overflowed Float32")
    return result


class ReceiverCore:
    """Contract-4 decoder. Only a complete, validated SCHEMA establishes a session.

    UDP SCHEMA_ACK is mandatory for each new table. The network client sends it
    after the on_schema callback succeeds. The sender gates FRAME on its receipt.
    A duplicate saved SCHEMA is acknowledged again; partial first receipt is not.
    'outbound' items are (kind, schema_id, sequence), with empty payloads.
    """
    def __init__(self, client_nonce: int, *, udp_size: int = MAX_UDP_SIZE,
                 fps: int = 60, transport: str = "udp", schema_ack: bool = True,
                 allow_push: bool = False) -> None:
        self.client_nonce = _uint(client_nonce, 64, "client nonce")
        if not client_nonce:
            raise ProtocolError("zero client nonce")
        hello_payload(fps, udp_size)
        if transport not in ("udp", "tcp"):
            raise ValueError("transport must be udp or tcp")
        self.transport = transport
        self.allow_push = allow_push
        if transport == "udp" and not schema_ack:
            raise ValueError("contract 4 requires SCHEMA_ACK on UDP")
        self.schema_ack = transport == "udp"
        self.requested_udp_size, self.requested_fps = udp_size, fps
        self.udp_size = udp_size
        self.session_id: int | None = None
        self.schema: Schema | None = None
        self.start_info: StartInfo | None = None
        self.actual_fps, self.lease_ms = 0, 10000
        self.latest_frame: Frame | None = None
        self.outbound: list[tuple[int, int, int]] = []
        self.stats = {"frames": 0, "unknown_schema": 0, "stale_frames": 0,
                      "wrong_session": 0, "sequence_gaps": 0, "schema_changes": 0}
        # One shared byte/group budget. Pre-session FRAME is ignored before reassembly.
        self._assembly = Reassembler()
        self._schema_wire = b""
        self._last_sequence: int | None = None
        self._last_request = self._last_ack = -math.inf
        self.last_valid_receive: float | None = None

    def _request_schema(self, schema_id: int, now: float) -> None:
        if self.session_id is not None and now - self._last_request >= 1.0:
            self.outbound.append((GET_SCHEMA, schema_id, 0))
            self._last_request = now

    def _ack_schema(self, schema_id: int, now: float, *, force: bool = False) -> None:
        if self.schema_ack and (force or now - self._last_ack >= 1.0):
            self.outbound.append((SCHEMA_ACK, schema_id, 0))
            self._last_ack = now

    def _validate_start_info(self, info: StartInfo) -> bool:
        if info.client_nonce != self.client_nonce and not (self.allow_push and info.client_nonce == 0):
            self.stats["wrong_session"] += 1
            return False
        if info.actual_fps > self.requested_fps or info.max_udp_size > self.requested_udp_size:
            raise ProtocolError("SCHEMA settings exceed the HELLO request")
        if self.start_info is not None and info != self.start_info:
            raise ProtocolError("session settings changed without a new session")
        return True

    def _accept_schema(self, packet: Packet, now: float) -> Schema | None:
        # Never replace an active session based on an unsolicited packet. Reconnect
        # creates a new ReceiverCore / client_nonce instead.
        if self.session_id is not None and packet.session_id != self.session_id:
            self.stats["wrong_session"] += 1
            return None
        if self.schema is not None and packet.schema_id < self.schema.schema_id:
            return None
        if packet.part_index == 0:
            info = StartInfo.parse(packet.payload)
            if not self._validate_start_info(info):
                return None
        if self.schema is not None and packet.schema_id == self.schema.schema_id:
            start = packet.part_index * (SCHEMA_UDP_SIZE - HEADER_SIZE) if packet.part_count > 1 else 0
            if (packet.total_length != len(self._schema_wire) or
                    packet.payload != self._schema_wire[start:start + len(packet.payload)]):
                raise ProtocolError("a schema id was reused with different contents")
            # We already hold the COMPLETE table. One valid repeated slice can
            # acknowledge that saved table, without parsing JSON again.
            self._ack_schema(packet.schema_id, now)
            self.last_valid_receive = now
            return None
        data = self._assembly.push(packet, now)
        if data is None:
            return None
        info = StartInfo.parse(data)
        if not self._validate_start_info(info):
            return None
        schema = Schema.parse(data[SCHEMA_INFO.size:], packet.session_id, packet.schema_id)
        # Commit only after EVERY byte and every field has passed validation.
        first_schema = self.session_id is None
        if first_schema:
            self.session_id = packet.session_id
            self.start_info = info
            self.actual_fps, self.udp_size, self.lease_ms = info.actual_fps, info.max_udp_size, info.lease_ms
        # A committed new table retires old, partially received motion data too.
        self._assembly.clear()
        self.outbound[:] = [item for item in self.outbound
                            if item[0] not in (SCHEMA_ACK, GET_SCHEMA)]
        self.schema, self._schema_wire = schema, data
        self.latest_frame = None
        self.last_valid_receive = now
        self.stats["schema_changes"] += 1
        self._ack_schema(schema.schema_id, now, force=True)
        return schema

    def accept(self, packet: Packet, now: float | None = None) -> Schema | Frame | None:
        now = time.monotonic() if now is None else now
        self._assembly.expire(now)
        if packet.kind == ERROR and packet.session_id == (self.session_id or self.client_nonce):
            try:
                reason = packet.payload.decode("utf-8")
            except UnicodeError as exc:
                raise ProtocolError("invalid error text") from exc
            raise RemoteError(repr(reason))
        if packet.kind == SCHEMA:
            return self._accept_schema(packet, now)
        if self.session_id is None or packet.session_id != self.session_id:
            self.stats["wrong_session"] += 1
            return None
        if packet.kind not in (FRAME, PONG, ERROR):
            raise ProtocolError("unexpected server-to-client message type")
        if packet.kind == PONG:
            self.last_valid_receive = now
            return None
        if packet.kind == FRAME:
            schema = self.schema
            if schema is None or packet.schema_id > schema.schema_id:
                self.stats["unknown_schema"] += 1
                self._request_schema(packet.schema_id, now)
                return None
            if packet.schema_id < schema.schema_id:
                self.stats["stale_frames"] += 1
                return None
            if self._last_sequence is not None and not is_newer_u32(packet.sequence, self._last_sequence):
                self.stats["stale_frames"] += 1
                return None
            if packet.total_length != schema.frame_bytes:
                raise ProtocolError("frame length differs from schema")
            data = self._assembly.push(packet, now)
            if data is None:
                return None
            values = schema.blend_struct.unpack_from(data)
            pose = POSE.unpack_from(data, schema.blend_struct.size)
            if not all(math.isfinite(value) for value in pose):
                raise ProtocolError("nonfinite pose in frame")
            if self._last_sequence is not None:
                self.stats["sequence_gaps"] += ((packet.sequence - self._last_sequence) & 0xFFFFFFFF) - 1
            self._last_sequence = packet.sequence
            frame = Frame(schema, packet.sequence, packet.source_token, bool(packet.flags & TRACKED),
                          bool(packet.flags & PLAYBACK), values, pose, now)
            self.latest_frame, self.last_valid_receive = frame, now
            self.stats["frames"] += 1
            return frame
        return None


@dataclass(frozen=True)
class RecoveryPolicy:
    """Monotonic-time limits. Waiting never closes the local receiving port.

    Defaults: HELLO at 0..4 s; FRAME repairs at 3,4,5 s; passive at 12 s.
    A schema/PONG does not restart the independent FRAME deadline.
    """
    frame_timeout: float = 3.0
    retry_interval: float = 1.0
    retry_count: int = 3
    wait_after: float = 12.0
    hello_count: int = 5
    hello_interval: float = 1.0

    def __post_init__(self) -> None:
        for name in ('frame_timeout', 'retry_interval', 'wait_after', 'hello_interval'):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be positive and finite')
        for name in ('retry_count', 'hello_count'):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 20:
                raise ValueError(f'{name} must be 1..20')
        if self.wait_after <= self.frame_timeout + self.retry_interval * (self.retry_count - 1):
            raise ValueError('wait_after must exceed the last repair time')


class FrameWatchdog:
    """Pure state machine: only a complete VALID FRAME resets the no-frame clock.

    RECV_SCHEMA/PONG/ACK retries/partial data never extend a running deadline.
    Repeated schemas received in WAITING are acknowledged by the client, but do
    not start another timer-driven retry cycle. A valid FRAME resumes streaming.
    """
    def __init__(self, policy: RecoveryPolicy | None = None, *, passive: bool = False):
        self.policy = policy or RecoveryPolicy()
        self.state = 'WAITING' if passive else 'WAIT_SCHEMA'
        self.since: float | None = None
        self.last_frame_at: float | None = None
        self.attempts = 0
        self.last_retry = -math.inf
        self._exhausted = False

    def schema_ready(self, now: float) -> None:
        if self.since is None:
            self.since = now
        if not self._exhausted:
            self.state = 'WAIT_FRAME'

    def frame_received(self, now: float) -> None:
        self.since = self.last_frame_at = now
        self.attempts = 0
        self.last_retry = -math.inf
        self.state = 'STREAMING'
        self._exhausted = False

    def wait(self) -> None:
        self.state = 'WAITING'
        self._exhausted = True

    def poll(self, now: float) -> str | None:
        if self.state == 'WAITING' or self.since is None:
            return None
        age = now - self.since
        if age >= self.policy.wait_after:
            self.wait()
            return 'wait'
        if (age >= self.policy.frame_timeout and self.attempts < self.policy.retry_count
                and now - self.last_retry >= self.policy.retry_interval):
            self.attempts += 1
            self.last_retry = now
            self.state = 'RECOVERING'
            return 'repair'
        return None


class V3Client:
    """Contract 4 receiver: bounded recovery, then persistent passive listening.

    UDP uses one bound UNCONNECTED socket and recvfrom/sendto. In TCP mode a PC
    listener stays open alongside any PC-initiated connection; a manual iOS
    sender connects to this listener. PC defaults: UDP 49983 / TCP 49986.
    The app parameter selects iOS port defaults; it is NOT authentication.

    --host restricts incoming packets/connections to that host's resolved IPs.
    No host requires listen_only=True (explicit opt-in to any peer on the LAN).
    An active stream cannot be taken over; only WAIT_SCHEMA/WAITING accept a new
    complete schema/session. Manual starts have StartInfo.client_nonce == 0.
    No authentication: use a trusted LAN/firewall. No background thread is made.
    """
    def __init__(self, host: str | None = None, *, transport: str = 'udp',
                 port: int | None = None, fps: int = 60, udp_size: int = MAX_UDP_SIZE,
                 reconnect: bool = True, connect_timeout: float = 3.0,
                 schema_ack: bool = True, listen_port: int | None = None,
                 bind: str = '0.0.0.0', listen_only: bool = False,
                 recovery: RecoveryPolicy | None = None, app: str = 'ifacialmocap') -> None:
        ios_default, pc_default = default_ports(app, transport)
        port = ios_default if port is None else port
        listen_port = pc_default if listen_port is None else listen_port
        self.app = app.lower()
        if transport not in ('udp', 'tcp'):
            raise ValueError('transport must be udp or tcp')
        if (type(port) is not int or type(listen_port) is not int or type(fps) is not int
                or not 1 <= port <= 65535 or not 0 <= listen_port <= 65535 or not 1 <= fps <= 60):
            raise ValueError('port 1..65535, listen_port 0..65535, fps 1..60 required')
        if not MIN_UDP_SIZE <= udp_size <= MAX_UDP_SIZE:
            raise ValueError('UDP size must be 576..1200')
        if not math.isfinite(connect_timeout) or connect_timeout <= 0:
            raise ValueError('connect_timeout must be positive and finite')
        if not host and not listen_only:
            raise ValueError('provide host or explicitly enable listen_only')
        if transport == 'udp' and not schema_ack:
            raise ValueError('contract 4 requires SCHEMA_ACK on UDP')
        self.host, self.transport, self.port = host, transport, port
        self.fps, self.udp_size = fps, udp_size
        self.connect_timeout, self.schema_ack = connect_timeout, transport == 'udp'
        # Kept for API compatibility. No-reconnect does not disable passive listening.
        self.reconnect = reconnect
        self.listen_port, self.bind, self.listen_only = listen_port, bind, listen_only
        self.policy = recovery or RecoveryPolicy()
        self.core: ReceiverCore | None = None
        self._sock: socket.socket | None = None
        self._listener: socket.socket | None = None
        self.local_port: int | None = None
        self.state = 'STOPPED'
        self.last_error: str | None = None
        self.watchdog = FrameWatchdog(self.policy, passive=listen_only)
        self._peer = None
        self._candidate = None  # (endpoint, session_id, core, start_time); at most one
        self._retired: list[tuple] = []  # bounded recently replaced endpoint/session pairs
        self._hello_nonce = 0
        self._hello_pending = False  # True only while a sent HELLO is unresolved.
        self._target = None
        self._allowed_ips: set[str] = set()
        self._last_ping = -math.inf
        self._ping_seq = 0
        self._on_state = None

    def _new_core(self) -> ReceiverCore:
        return ReceiverCore(self._hello_nonce, transport=self.transport, fps=self.fps,
                            udp_size=self.udp_size, allow_push=True)

    def _set_state(self, reason: str = '') -> None:
        state = self.watchdog.state
        if self.state != state:
            self.state = state
            LOG.info('State=%s%s', state, f': {reason}' if reason else '')
            if self._on_state:
                _invoke_callback(self._on_state, state)

    def _wait(self, reason: str) -> None:
        self._hello_pending = False
        self.watchdog.wait()
        if self.core is not None:
            self.core.outbound.clear()
            self.core.latest_frame = None
        self._set_state(reason)

    def _resolve(self) -> int:
        # Select the family by --bind. Explicit :: supports an IPv6 listener.
        family = socket.AF_INET6 if ':' in self.bind else socket.AF_INET
        kind = socket.SOCK_DGRAM if self.transport == 'udp' else socket.SOCK_STREAM
        if self.host:
            results = socket.getaddrinfo(self.host, self.port, family, kind)
            self._allowed_ips = {item[4][0] for item in results}
            self._target = results[0][4]
        return family

    def _allowed(self, endpoint) -> bool:
        return bool(endpoint) and (not self._allowed_ips or endpoint[0] in self._allowed_ips)

    def _open_receiver(self, family: int) -> socket.socket:
        kind = socket.SOCK_DGRAM if self.transport == 'udp' else socket.SOCK_STREAM
        sock = socket.socket(family, kind)
        try:
            if self.transport == 'tcp':
                # Windows exclusive binding avoids a second process hijacking the port.
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((self.bind, self.listen_port))
            self.local_port = sock.getsockname()[1]
            if self.transport == 'tcp':
                sock.listen(4)
            sock.setblocking(False)
            return sock
        except BaseException:
            sock.close()
            raise

    def _send(self, kind: int, payload: bytes = b'', *, schema_id: int = 0,
              sequence: int = 0) -> None:
        assert self.core is not None
        session = self._hello_nonce if kind == HELLO else self.core.session_id
        if session is None or self._sock is None:
            return
        target = self._target if kind == HELLO else self._peer
        for raw in encode_message(kind, payload, session_id=session, schema_id=schema_id,
                                  sequence=sequence,
                                  udp_size=self.core.udp_size if self.transport == 'udp' else None):
            if self.transport == 'udp':
                if target is not None and self._sock.sendto(raw, target) != len(raw):
                    raise OSError('partial UDP send')
            else:
                self._sock.sendall(raw)
        if kind == HELLO and target is not None:
            self._hello_pending = True

    def _flush_controls(self, *, passive: bool = False) -> None:
        assert self.core is not None
        while self.core.outbound:
            kind, schema, seq = self.core.outbound.pop(0)
            if passive and kind != SCHEMA_ACK:
                continue
            self._send(kind, schema_id=schema, sequence=seq)

    def _tick(self, now: float) -> None:
        assert self.core is not None
        self.core._assembly.expire(now)
        if self._candidate and now - self._candidate[3] >= 3.0:
            self._candidate = None
        action = self.watchdog.poll(now)
        if action == 'wait':
            self._wait('No complete FRAME; receive port stays open. Waiting for iOS.')
            return
        if action == 'repair' and self.core.schema is not None:
            # Re-ACK ONLY the complete saved table. Ask for the newest table too.
            self.core._ack_schema(self.core.schema.schema_id, now)
            self.core._request_schema(0, now)
            self._flush_controls()
            self._set_state(f'FRAME recovery {self.watchdog.attempts}/{self.policy.retry_count}')
        if self.watchdog.state == 'WAITING' or self.core.session_id is None:
            return
        if self.core.last_valid_receive is not None:
            if now - self.core.last_valid_receive >= self.core.lease_ms / 1000.0:
                self._wait('No valid traffic; keeping the receiving port open')
                return
        if now - self._last_ping >= 3.0:
            self._ping_seq = (self._ping_seq + 1) & 0xFFFFFFFF
            self._send(PING, sequence=self._ping_seq)
            self._last_ping = now

    def _handle(self, packet: Packet, peer, now: float, on_frame, on_schema) -> None:
        assert self.core is not None
        if not self._allowed(peer):
            return
        key = (peer, packet.session_id)
        if key in self._retired:
            return
        current = (peer == self._peer and packet.session_id == self.core.session_id)
        if not current:
            # A HELLO refusal belongs only to the still-pending start request.
            # Never apply a delayed refusal to an adopted server session, and
            # never accept one from a different endpoint (even on the same IP).
            # Current-session ERROR messages use ReceiverCore below instead.
            if (packet.kind == ERROR and self._hello_pending
                    and self.core.session_id is None
                    and self.watchdog.state == 'WAIT_SCHEMA'
                    and not self.listen_only and peer == self._target
                    and packet.session_id == self._hello_nonce):
                self.last_error = repr(packet.payload.decode('utf-8', 'replace'))
                LOG.warning('iOS declined request: %s; listener remains open', self.last_error)
                self._wait('iOS declined request')
                return
            if packet.kind != SCHEMA or self.watchdog.state not in ('WAIT_SCHEMA', 'WAITING'):
                return
            if self._candidate is None or self._candidate[:2] != (peer, packet.session_id):
                # Do not let other endpoints continuously evict an incomplete candidate.
                if self._candidate is not None and now - self._candidate[3] < 3.0:
                    return
                self._candidate = (peer, packet.session_id, self._new_core(), now)
            candidate = self._candidate[2]
            event = candidate.accept(packet, now)
            if not isinstance(event, Schema):
                return
            # Prepare the consumer first. No ACK, state commit or ownership switch on error.
            if on_schema:
                _invoke_callback(on_schema, event)
            if self.core.session_id is not None:
                self._retired.append((self._peer, self.core.session_id))
                self._retired = self._retired[-32:]
            self._hello_pending = False
            self.core, self._peer = candidate, peer
            self._candidate = None
            self._last_ping = now
            self.watchdog.schema_ready(now)
            LOG.info('Schema %d: %s, %d blend shapes, %s (manual=%s)', event.schema_id,
                     event.app, len(event.blend_names), event.blend_encoding,
                     candidate.start_info.client_nonce == 0)
            self._flush_controls(passive=self.watchdog.state == 'WAITING')
            self._set_state()
            return
        try:
            event = self.core.accept(packet, now)
        except RemoteError as exc:
            self.last_error = str(exc)
            LOG.warning('iOS error: %s; receiver stays open', exc)
            self._wait('iOS stopped or rejected the session')
            return
        if isinstance(event, Schema):
            if on_schema:
                _invoke_callback(on_schema, event)
            self.watchdog.schema_ready(now)
            LOG.info('Schema %d: %s, %d blend shapes, %s', event.schema_id,
                     event.app, len(event.blend_names), event.blend_encoding)
        elif isinstance(event, Frame):
            _invoke_callback(on_frame, event)
            self.watchdog.frame_received(now)
            if self.state == 'WAITING':
                self._last_ping = now
        self._flush_controls(passive=self.watchdog.state == 'WAITING')
        self._set_state()

    def _run_udp(self, stopped, on_frame, on_schema, start: float) -> None:
        assert self._sock is not None
        attempts = 0
        next_hello = start
        last_bad = -math.inf
        while not stopped():
            now = time.monotonic()
            if self.watchdog.state == 'WAIT_SCHEMA' and not self.listen_only:
                if attempts < self.policy.hello_count and now >= next_hello:
                    try:
                        self._send(HELLO, hello_payload(self.fps, self.udp_size))
                    except OSError as exc:
                        self.last_error = str(exc)
                    attempts += 1
                    next_hello = now + self.policy.hello_interval
                elif attempts >= self.policy.hello_count and now >= next_hello:
                    self._wait('HELLO attempts exhausted; waiting for an iOS SCHEMA')
            try:
                self._tick(now)
            except OSError as exc:
                self.last_error = str(exc)
                # A temporary send/ICMP error must not rebind/change the receive port.
            try:
                ready, _, _ = select.select([self._sock], [], [], 0.05 if self.state != 'WAITING' else 0.2)
                if not ready:
                    continue
                raw, peer = self._sock.recvfrom(65536)
                if not self._allowed(peer):
                    continue
                # Bootstrap SCHEMA always uses <=576; validate FRAME using the adopted limit.
                limit = self.core.udp_size if peer == self._peer else self.udp_size
                packet = decode_packet(raw, max_udp_size=limit)
                self._handle(packet, peer, time.monotonic(), on_frame, on_schema)
            except (ProtocolError, OSError) as exc:
                if time.monotonic() - last_bad >= 1.0:
                    LOG.warning('Ignoring UDP packet/socket error: %s', exc)
                    last_bad = time.monotonic()
                self.last_error = str(exc)

    def _run_tcp(self, stopped, on_frame, on_schema, start: float, family: int) -> None:
        assert self._listener is not None
        framer = TCPFramer()
        peer = None
        connecting = False
        outgoing = False
        conn_started = start
        attempts = 0
        next_connect = start
        hello_sent = False

        def close_connection(reason: str) -> None:
            nonlocal connecting, hello_sent, framer, peer
            self._hello_pending = False
            if self._sock:
                self._sock.close()
            self._sock = None
            connecting = hello_sent = False
            framer, peer = TCPFramer(), None
            # A finite initial burst is allowed; after adoption there is only listening.
            if self.core.session_id is not None:
                self._wait(reason)

        while not stopped():
            now = time.monotonic()
            if (self._sock is None and self.watchdog.state == 'WAIT_SCHEMA' and not self.listen_only
                    and attempts < self.policy.hello_count and now >= next_connect):
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.setblocking(False)
                error = sock.connect_ex(self._target)
                self._sock, connecting, outgoing = sock, True, True
                peer, conn_started, hello_sent = self._target, now, False
                attempts += 1
                next_connect = now + self.policy.hello_interval
                if error not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                                 getattr(errno, 'WSAEWOULDBLOCK', 10035)):
                    close_connection('TCP connection failed')
            if (self._sock is None and attempts >= self.policy.hello_count and now >= next_connect
                    and self.watchdog.state == 'WAIT_SCHEMA'):
                self._wait('TCP attempts exhausted; PC listener remains open')
            if self._sock is not None and not connecting:
                try:
                    self._tick(now)
                except OSError as exc:
                    self.last_error = str(exc)
                    close_connection('TCP send failed; waiting for iOS to connect')
            if self._sock is not None:
                if connecting and now - conn_started >= self.connect_timeout:
                    close_connection('TCP connect timeout')
                elif (self.core.session_id is None or peer != self._peer) and now - conn_started >= 5.0:
                    close_connection('Incomplete initial SCHEMA; listener remains open')
            readers = [self._listener]
            writers = []
            if self._sock:
                (writers if connecting else readers).append(self._sock)
            ready, writable, _ = select.select(readers, writers, [], 0.05 if self.state != 'WAITING' else 0.2)
            if self._listener in ready:
                conn, address = self._listener.accept()
                if not self._allowed(address) or self.watchdog.state not in ('WAIT_SCHEMA', 'WAITING'):
                    conn.close()
                else:
                    # Never let a connection alone displace a live stream.
                    close_connection('Manual TCP connection received')
                    self._sock, peer = conn, address
                    conn.settimeout(0.5)
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conn_started, connecting, outgoing, hello_sent = time.monotonic(), False, False, True
                    self._candidate = None
                    framer = TCPFramer()
            if self._sock in writable and connecting:
                error = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error:
                    close_connection('TCP connect failed')
                else:
                    self._sock.settimeout(0.5)
                    self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    connecting = False
                    try:
                        self._send(HELLO, hello_payload(self.fps, self.udp_size))
                        hello_sent = True
                    except OSError as exc:
                        self.last_error = str(exc)
                        close_connection('HELLO send failed')
            if self._sock in ready and not connecting:
                try:
                    raw = self._sock.recv(65536)
                    if not raw:
                        framer.eof()
                        close_connection('TCP disconnected; listener remains open')
                        continue
                    for packet in framer.feed(raw):
                        self._handle(packet, peer, time.monotonic(), on_frame, on_schema)
                except (ProtocolError, OSError) as exc:
                    self.last_error = str(exc)
                    close_connection('Invalid/closed TCP stream; waiting for iOS')

    def run(self, on_frame: Callable[[Frame], None], *,
            on_schema: Callable[[Schema], None] | None = None,
            on_state: Callable[[str], None] | None = None,
            stop_event: threading.Event | None = None, duration: float | None = None) -> None:
        """Run until explicitly stopped. A silent sender does NOT terminate/rebind.

        Port 0 is supported for automated tests, but manual iOS configuration
        should use UDP 49983 / TCP 49986 or an explicitly chosen local port.
        Consumer callback failures are fatal; no acknowledgement is sent for a
        failing on_schema callback. OS bind failures are also reported as errors.
        """
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise ValueError('duration must be positive')
        stop_event = stop_event or threading.Event()
        start = time.monotonic()
        deadline = None if duration is None else start + duration
        def stopped():
            return stop_event.is_set() or (deadline is not None and time.monotonic() >= deadline)
        self._on_state = on_state
        self.watchdog = FrameWatchdog(self.policy, passive=self.listen_only)
        self._hello_nonce = secrets.randbits(64) or 1
        self._hello_pending = False
        self.core = self._new_core()
        self._peer = self._candidate = None
        self._retired.clear()
        self._last_ping = start
        family = self._resolve()
        receiver = self._open_receiver(family)
        LOG.info('Receiving %s on %s:%d; manual iOS target is this PC and this port',
                 self.transport.upper(), self.bind, self.local_port)
        try:
            self._set_state()
            if self.transport == 'udp':
                self._sock = receiver
                self._run_udp(stopped, on_frame, on_schema, start)
            else:
                self._listener = receiver
                self._sock = None
                self._run_tcp(stopped, on_frame, on_schema, start, family)
        finally:
            self._hello_pending = False
            # STOP only on deliberate exit/callback failure, never on passive transition.
            if self.core and self.core.session_id and self._sock:
                try:
                    self._send(STOP)
                except (OSError, ProtocolError):
                    pass
            if self._sock:
                self._sock.close()
            receiver.close()
            self._sock = self._listener = None
            self.state = 'STOPPED'

def format_frame_log(frame: Frame, received_frames: int) -> str:
    """Format every decoded value as one line for console diagnostics.

    Call only when a console update is due, not on every incoming frame.
    BlendShape names and raw integer values follow the SCHEMA order. No values
    are clipped, normalized, or omitted. Float values use Python's repr without
    the old three-decimal rounding. Quoted JSON names are ASCII-escaped so that
    custom names cannot inject line breaks/control characters into the log.

    This function returns NO trailing newline. The logging handler supplies one.
    Applications should consume Frame fields directly instead of parsing logs.
    """
    if len(frame.schema.blend_names) != len(frame.blend_values):
        raise ValueError("schema and BlendShape value counts differ")
    blend_shapes = json.dumps(
        dict(zip(frame.schema.blend_names, frame.blend_values)),
        ensure_ascii=True, allow_nan=False, separators=(",", ":"),
    )
    return (
        f"frames={received_frames} seq={frame.sequence} "
        f"tracked={frame.tracking} count={len(frame.blend_values)} "
        f"playback={frame.playback} app={frame.schema.app} "
        f"session_id={frame.schema.session_id} schema_id={frame.schema.schema_id} "
        f"source_token={frame.source_token} blend_encoding={frame.schema.blend_encoding} "
        f"blendShapes={blend_shapes} "
        f"head={frame.head!r} rightEye={frame.right_eye!r} leftEye={frame.left_eye!r}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="iPhone/iPad IP; also limits allowed sender IPs")
    parser.add_argument("--listen", action="store_true", help="passive from launch; no HELLO")
    parser.add_argument("--bind", default="0.0.0.0", help="PC bind address; use :: for IPv6")
    parser.add_argument("--listen-port", type=int, default=None, help="PC receiving port (default: UDP 49983 / TCP 49986)")
    parser.add_argument("--transport", choices=("udp", "tcp"), default="udp")
    parser.add_argument("--port", type=int, default=None, help="iOS port; default is selected by --app and --transport")
    parser.add_argument("--app", choices=tuple(PORT_PROFILES), default="ifacialmocap", help="select iOS standard ports; default: ifacialmocap")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--udp-size", type=int, default=MAX_UDP_SIZE)
    parser.add_argument("--duration", type=float, help="stop after N seconds")
    parser.add_argument("--no-reconnect", action="store_true", help="legacy alias: passive listening is always retained")
    parser.add_argument("--jsonl", help="new file for one JSON object per frame; refuses overwrite (extra CPU/I/O)")
    parser.add_argument("--log-every", type=float, default=1.0, help="all-value console log interval in seconds; 0 disables logging, not reception")
    args = parser.parse_args(argv)
    if not args.host and not args.listen:
        parser.error("provide --host PHONE_IP or --listen")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not math.isfinite(args.log_every) or args.log_every < 0:
        parser.error("--log-every must be nonnegative")
    output = None
    frames = 0
    last_log = -math.inf
    try:
        client = V3Client(args.host, transport=args.transport, port=args.port, fps=args.fps,
                          udp_size=args.udp_size, reconnect=not args.no_reconnect,
                          listen_port=args.listen_port, bind=args.bind, listen_only=args.listen, app=args.app)
        if args.jsonl:
            output = open(args.jsonl, "x", encoding="utf-8", buffering=65536)
        def on_frame(frame: Frame) -> None:
            nonlocal frames, last_log
            frames += 1
            if output:
                output.write(json.dumps(frame.to_dict(), ensure_ascii=False, allow_nan=False) + "\n")
            now = time.monotonic()
            if args.log_every and now - last_log >= args.log_every:
                # All values from this frame, on one line. StreamHandler adds
                # exactly one trailing newline; do not add a blank line here.
                LOG.info("%s", format_frame_log(frame, frames))
                last_log = now
        client.run(on_frame, duration=args.duration)
    except KeyboardInterrupt:
        LOG.info("Stopped")
    except (OSError, ProtocolError, RemoteError, CallbackError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    finally:
        if output:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
