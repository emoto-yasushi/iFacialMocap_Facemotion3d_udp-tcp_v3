#!/usr/bin/env python3
"""Face Motion v3 (FMV3) receiver for iFacialMocap, iFacialMocapTr and Facemotion3d.

Wire contract revision 5. Python 3.10+, standard library only.

    UDP (recommended)  PC --HELLO--> iOS --SCHEMA--> PC --SCHEMA_ACK--> iOS --FRAME...--> PC
    TCP (alternative)  PC connects to iOS, then HELLO --> SCHEMA --> FRAME...  (no SCHEMA_ACK)
    Manual start       the user enters this PC's address in the iOS app; iOS sends SCHEMA first

Command line (the same for all three apps; Facemotion3d needs its "Other" or "Unity" license):
    python face_motion_v3.py --host PHONE_IP                  UDP, the PC starts the stream
    python face_motion_v3.py --host PHONE_IP --transport tcp  TCP, the PC starts the stream
    python face_motion_v3.py --listen                         wait for a manual start from iOS

Library:
    V3Client("PHONE_IP").run(on_frame, on_schema=on_schema)

The receiver never exits because iOS went quiet: after a few bounded retries it
keeps its port open and waits (state WAITING) until iOS starts again.

PROTOCOL_V3.md is the specification. This file is organised in the same order:
  1. Constants and ports      4. One accepted session (no sockets)
  2. Binary codec             5. Network client (UDP and TCP)
  3. Message bodies           6. Console output and command line
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import math
import secrets
import select
import socket
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

LOG = logging.getLogger("face_motion_v3")

# =============================================================================
# 1. Constants and ports
# =============================================================================

MAGIC = b"FMV3"
CONTRACT_REVISION = 5

# Message types (header byte 4): what a message is. Checked first on receipt.
HELLO = 1        # PC -> iOS   start request
SCHEMA = 2       # iOS -> PC   BlendShape name list + value type + session settings
SCHEMA_ACK = 3   # PC -> iOS   "the complete SCHEMA is stored" (UDP only)
FRAME = 4        # iOS -> PC   one set of values in SCHEMA order
GET_SCHEMA = 5   # PC -> iOS   please resend the current SCHEMA
PING = 6         # PC -> iOS   keepalive
PONG = 7         # iOS -> PC   keepalive reply
STOP = 8         # PC -> iOS   end this session
ERROR = 9        # iOS -> PC   refusal or error, UTF-8 text
TYPE_NAMES = MappingProxyType({
    HELLO: "HELLO", SCHEMA: "SCHEMA", SCHEMA_ACK: "SCHEMA_ACK", FRAME: "FRAME",
    GET_SCHEMA: "GET_SCHEMA", PING: "PING", PONG: "PONG", STOP: "STOP", ERROR: "ERROR",
})
TRACKED, PLAYBACK = 0x01, 0x02  # FRAME header flags

# (iOS listening port, PC listening port), the same for iFacialMocap,
# iFacialMocapTr and Facemotion3d ("Other" or "Unity" license; v3 sends the
# iFacialMocap-compatible "Other" output with either).
# A normal start connects to the iOS port; a manual start from iOS connects to
# the PC port. Each side listens on exactly one port per transport.
PORTS = MappingProxyType({"udp": (49983, 49983), "tcp": (49984, 49984)})

HEADER = struct.Struct("<4sBBHQIIIIHHI")  # 40 bytes, little-endian, no padding
HEADER_SIZE = HEADER.size
HELLO_BODY = struct.Struct("<HHI")        # requested_fps, max_udp_size, contract_revision
START_INFO = struct.Struct("<QHHII")      # client_nonce, actual_fps, max_udp_size, lease_ms, contract_revision
POSE = struct.Struct("<12f")              # head rx ry rz px py pz, right eye rx ry rz, left eye rx ry rz
POSE_LAYOUT = "head_rxyz_pxyz_rightEye_rxyz_leftEye_rxyz"

SCHEMA_DATAGRAM_SIZE = 576                # every UDP SCHEMA datagram, header included
MIN_UDP_SIZE, MAX_UDP_SIZE = 576, 1200    # negotiated FRAME datagram size, header included
MAX_SCHEMA_PAYLOAD = 262_144              # start information + JSON
MAX_BLENDSHAPES = 4096
MAX_NAME_BYTES = 255
MAX_FRAME_PAYLOAD = MAX_BLENDSHAPES * 4 + POSE.size
MAX_ERROR_BYTES = 512
MAX_FRAGMENTS = 512
MAX_REASSEMBLY_GROUPS = 8
MAX_REASSEMBLY_BYTES = 524_288
LEASE_MS_RANGE = (5000, 60000)
EMPTY_CONTROLS = (GET_SCHEMA, PING, PONG, STOP, SCHEMA_ACK)

# Fixed protocol timing (seconds). The adjustable receiver timers are in RecoveryPolicy.
CONTROL_MIN_INTERVAL = 1.0        # re-ACK of a stored SCHEMA and GET_SCHEMA, per session
CANDIDATE_TIMEOUT = 3.0           # an incomplete first SCHEMA of a new session
FRAME_REASSEMBLY_TIMEOUT = 0.25
SCHEMA_REASSEMBLY_TIMEOUT = 3.0
RETIRED_SESSIONS = 32             # replaced sessions whose late packets are ignored


class ProtocolError(ValueError):
    """Malformed or inconsistent FMV3 data. The affected message is never used."""


class RemoteError(RuntimeError):
    """iOS refused the request or ended the session with an ERROR message."""


class CallbackError(RuntimeError):
    """on_frame / on_schema / on_state raised. Stops the receiver; not a network error."""


class PortInUseError(OSError):
    """The PC port is already bound by another program. No other port is tried."""


def default_ports(transport: str = "udp") -> tuple[int, int]:
    """Return (iOS port, PC port) for "udp" or "tcp"."""
    if transport not in PORTS:
        raise ValueError("transport must be udp or tcp")
    return PORTS[transport]


def is_newer_u32(candidate: int, previous: int) -> bool:
    """UInt32 sequence comparison with wraparound; half the range is ambiguous (not newer)."""
    delta = (candidate - previous) & 0xFFFFFFFF
    return 0 < delta < 0x80000000


def _uint(value: int, bits: int, label: str) -> int:
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ProtocolError(f"{label} must be an unsigned {bits}-bit integer")
    return value


# =============================================================================
# 2. Binary codec: header, UDP fragments, TCP stream
# =============================================================================

@dataclass(frozen=True)
class Packet:
    """One decoded datagram / TCP message. `payload` is this packet's chunk."""
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
    """Validate the 40-byte header against the per-type rules of PROTOCOL_V3.md section 3."""
    if len(data) < HEADER_SIZE:
        raise ProtocolError("incomplete header")
    (magic, kind, flags, size, session, schema_id, sequence, token,
     total, index, count, length) = HEADER.unpack_from(data)
    if magic != MAGIC or size != HEADER_SIZE:
        raise ProtocolError("not an FMV3 message (magic/header size)")
    if kind not in TYPE_NAMES:
        hint = " (revision 4 used 10 for SCHEMA_ACK)" if kind == 10 else ""
        raise ProtocolError(f"unknown message type {kind}{hint}")
    if kind == FRAME:
        if flags & ~(TRACKED | PLAYBACK):
            raise ProtocolError("unknown FRAME flag bits")
    elif flags:
        raise ProtocolError("flags are only used on FRAME")
    if session == 0:
        raise ProtocolError("session_id is zero")
    if not 1 <= count <= MAX_FRAGMENTS or index >= count:
        raise ProtocolError("invalid part_index/part_count")
    if kind in (SCHEMA, FRAME, SCHEMA_ACK):
        if schema_id == 0:
            raise ProtocolError(f"{TYPE_NAMES[kind]} needs a nonzero schema_id")
    elif kind != GET_SCHEMA and schema_id != 0:
        raise ProtocolError(f"{TYPE_NAMES[kind]} must have schema_id 0")
    if kind != FRAME and token != 0:
        raise ProtocolError("source_token is only used on FRAME")
    if kind in (HELLO, SCHEMA, GET_SCHEMA, STOP, SCHEMA_ACK) and sequence != 0:
        raise ProtocolError(f"{TYPE_NAMES[kind]} must have sequence 0")
    if length > total:
        raise ProtocolError("chunk_length exceeds total_payload_length")
    if kind == HELLO and total != HELLO_BODY.size:
        raise ProtocolError("HELLO payload must be 8 bytes")
    if kind in EMPTY_CONTROLS and total != 0:
        if kind == SCHEMA_ACK:   # revision 4 numbered SCHEMA 3
            raise ProtocolError("type 3 with a payload: this looks like a SCHEMA from an iOS app using "
                                "FMV3 revision 4 (old beta). Update the iOS app.")
        raise ProtocolError(f"{TYPE_NAMES[kind]} has no payload")
    if kind == SCHEMA and not START_INFO.size <= total <= MAX_SCHEMA_PAYLOAD:
        raise ProtocolError("SCHEMA payload size out of range")
    if kind == FRAME and not POSE.size <= total <= MAX_FRAME_PAYLOAD:
        raise ProtocolError("FRAME payload size out of range")
    if kind == ERROR and not 1 <= total <= MAX_ERROR_BYTES:
        raise ProtocolError("ERROR text must be 1-512 bytes")
    if count == 1:
        if index != 0 or length != total:
            raise ProtocolError("single-part message with a partial chunk")
    elif kind not in (SCHEMA, FRAME) or length == 0 or count > total:
        raise ProtocolError("only SCHEMA and FRAME may be split into parts")
    return kind, flags, session, schema_id, sequence, token, total, index, count, length


def decode_packet(data: bytes, *, max_udp_size: int | None = None) -> Packet:
    """Decode one message. Pass max_udp_size for UDP to enforce canonical fragments."""
    kind, flags, session, schema_id, sequence, token, total, index, count, length = _read_header(data)
    if len(data) != HEADER_SIZE + length:
        raise ProtocolError("message length does not match chunk_length")
    if max_udp_size is not None:
        limit = SCHEMA_DATAGRAM_SIZE if kind == SCHEMA else max_udp_size
        if len(data) > limit:
            raise ProtocolError(f"UDP datagram larger than {limit} bytes")
        capacity = limit - HEADER_SIZE
        if count != max(1, math.ceil(total / capacity)) or length != min(capacity, total - index * capacity):
            raise ProtocolError("UDP fragments are not split at the fixed capacity")
    payload = bytes(data[HEADER_SIZE:])
    if kind == ERROR:
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("ERROR text is not valid UTF-8") from exc
    return Packet(kind, flags, session, schema_id, sequence, token, total, index, count, payload)


def encode_message(kind: int, payload: bytes = b"", *, session_id: int, schema_id: int = 0,
                   sequence: int = 0, token: int = 0, flags: int = 0,
                   udp_size: int | None = None) -> list[bytes]:
    """Return one TCP message (udp_size=None) or the canonical UDP datagrams."""
    for value, bits, name in ((session_id, 64, "session_id"), (schema_id, 32, "schema_id"),
                              (sequence, 32, "sequence"), (token, 32, "source_token")):
        _uint(value, bits, name)
    if udp_size is None:
        capacity = max(1, len(payload))
    elif not MIN_UDP_SIZE <= udp_size <= MAX_UDP_SIZE:
        raise ProtocolError("UDP size must be 576-1200")
    elif kind == SCHEMA:
        capacity = SCHEMA_DATAGRAM_SIZE - HEADER_SIZE
    elif kind == FRAME:
        capacity = udp_size - HEADER_SIZE
    else:
        capacity = max(1, len(payload))
    count = max(1, math.ceil(len(payload) / capacity))
    if count > MAX_FRAGMENTS:
        raise ProtocolError("message needs more than 512 UDP fragments")
    parts = []
    for index in range(count):
        chunk = payload[index * capacity:(index + 1) * capacity]
        parts.append(HEADER.pack(MAGIC, kind, flags, HEADER_SIZE, session_id, schema_id, sequence,
                                 token, len(payload), index, count, len(chunk)) + chunk)
    return parts


class TCPFramer:
    """Split a TCP byte stream into messages (header + chunk_length bytes).

    TCP messages are never split into parts. A malformed header ends the stream;
    the caller closes that connection instead of searching for the next "FMV3".
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        self._buffer += data
        packets, offset = [], 0
        while len(self._buffer) - offset >= HEADER_SIZE:
            fields = _read_header(memoryview(self._buffer)[offset:offset + HEADER_SIZE])
            if fields[8] != 1:
                raise ProtocolError("TCP messages must not be split into parts")
            end = offset + HEADER_SIZE + fields[9]
            if end > len(self._buffer):
                break
            packets.append(decode_packet(bytes(self._buffer[offset:end])))
            offset = end
        del self._buffer[:offset]
        return packets

    @property
    def has_partial_message(self) -> bool:
        return bool(self._buffer)


@dataclass
class _Parts:
    first: Packet
    created: float
    chunks: dict[int, bytes] = field(default_factory=dict)
    size: int = 0


class Reassembler:
    """Join UDP fragments of SCHEMA / FRAME within fixed memory and time limits.

    Incomplete FRAMEs expire after 0.25 s and SCHEMAs after 3 s; nothing is ever
    retransmitted or guessed. Identical duplicates are ignored; conflicts drop the set.
    """

    def __init__(self) -> None:
        self._groups: OrderedDict[tuple, _Parts] = OrderedDict()
        self._bytes = 0

    def _drop(self, key: tuple) -> None:
        group = self._groups.pop(key, None)
        if group is not None:
            self._bytes -= group.size

    def clear(self) -> None:
        self._groups.clear()
        self._bytes = 0

    def expire(self, now: float) -> None:
        for key, group in list(self._groups.items()):
            limit = SCHEMA_REASSEMBLY_TIMEOUT if group.first.kind == SCHEMA else FRAME_REASSEMBLY_TIMEOUT
            if now - group.created >= limit:
                self._drop(key)

    def push(self, packet: Packet, now: float) -> bytes | None:
        """Return the complete payload once every part has arrived, otherwise None."""
        self.expire(now)
        if packet.part_count == 1:
            return packet.payload
        key = (packet.session_id, packet.kind, packet.schema_id, packet.sequence)
        group = self._groups.get(key)
        if group is None:
            while len(self._groups) >= MAX_REASSEMBLY_GROUPS:
                self._drop(next(iter(self._groups)))
            group = self._groups[key] = _Parts(packet, now)
        first = group.first
        if (first.flags, first.source_token, first.total_length, first.part_count) != (
                packet.flags, packet.source_token, packet.total_length, packet.part_count):
            self._drop(key)
            raise ProtocolError("fragments of one message disagree")
        previous = group.chunks.get(packet.part_index)
        if previous is not None:
            if previous != packet.payload:
                self._drop(key)
                raise ProtocolError("duplicate fragment with different bytes")
            return None
        if group.size + len(packet.payload) > packet.total_length:
            self._drop(key)
            raise ProtocolError("fragments exceed total_payload_length")
        while self._bytes + len(packet.payload) > MAX_REASSEMBLY_BYTES:
            oldest = next((k for k in self._groups if k != key), None)
            if oldest is None:
                self._drop(key)
                raise ProtocolError("reassembly memory limit reached")
            self._drop(oldest)
        group.chunks[packet.part_index] = packet.payload
        group.size += len(packet.payload)
        self._bytes += len(packet.payload)
        if len(group.chunks) < packet.part_count:
            return None
        self._drop(key)
        payload = b"".join(group.chunks[i] for i in range(packet.part_count))
        if len(payload) != packet.total_length:
            raise ProtocolError("reassembled length mismatch")
        return payload


# =============================================================================
# 3. Message bodies: HELLO, SCHEMA (start information + JSON), FRAME
# =============================================================================

def hello_payload(fps: int = 60, udp_size: int = MAX_UDP_SIZE) -> bytes:
    """HELLO body: requested FPS limit, largest FRAME datagram, contract revision."""
    if type(fps) is not int or not 1 <= fps <= 60:
        raise ProtocolError("requested fps must be 1-60")
    if type(udp_size) is not int or not MIN_UDP_SIZE <= udp_size <= MAX_UDP_SIZE:
        raise ProtocolError("requested UDP size must be 576-1200")
    return HELLO_BODY.pack(fps, udp_size, CONTRACT_REVISION)


@dataclass(frozen=True)
class StartInfo:
    """The first 20 bytes of every SCHEMA payload. Fixed for the whole session."""
    client_nonce: int   # the HELLO nonce for a normal start, 0 for a manual start
    actual_fps: int
    max_udp_size: int
    lease_ms: int

    @classmethod
    def parse(cls, payload: bytes) -> StartInfo:
        if len(payload) < START_INFO.size:
            raise ProtocolError("SCHEMA start information is incomplete")
        nonce, fps, udp_size, lease_ms, revision = START_INFO.unpack_from(payload)
        if revision != CONTRACT_REVISION:
            raise ProtocolError(
                f"iOS uses FMV3 contract revision {revision}; this receiver needs revision "
                f"{CONTRACT_REVISION}. Update the iOS app (old beta builds used revision 4).")
        if not 1 <= fps <= 60 or not MIN_UDP_SIZE <= udp_size <= MAX_UDP_SIZE:
            raise ProtocolError("SCHEMA start information out of range")
        if not LEASE_MS_RANGE[0] <= lease_ms <= LEASE_MS_RANGE[1]:
            raise ProtocolError("SCHEMA lease_ms out of range")
        return cls(nonce, fps, udp_size, lease_ms)

    def pack(self) -> bytes:
        return START_INFO.pack(_uint(self.client_nonce, 64, "client_nonce"), self.actual_fps,
                               self.max_udp_size, self.lease_ms, CONTRACT_REVISION)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _no_nan(value: str) -> None:
    raise ProtocolError(f"JSON constant {value} is not allowed")


# Keys whose value changes how FRAME bytes are read. An unknown value is rejected.
_FIXED_SCHEMA_VALUES = MappingProxyType({
    "blend_unit": "percent", "pose_layout": POSE_LAYOUT,
    "rotation_unit": "degree", "position_unit": "meter",
})


@dataclass(frozen=True)
class Schema:
    """One SCHEMA: the BlendShape name list in FRAME order and how to read the values."""
    session_id: int
    schema_id: int
    app: str
    profile: str
    blend_names: tuple[str, ...]
    blend_encoding: str                 # "i16" or "i32"
    index_by_name: Mapping[str, int]
    blend_struct: struct.Struct

    @property
    def frame_bytes(self) -> int:
        return self.blend_struct.size + POSE.size

    @classmethod
    def parse(cls, json_bytes: bytes, session_id: int = 1, schema_id: int = 1) -> Schema:
        """Parse the schema JSON (the SCHEMA payload after its 20-byte start information).

        Forward compatible: unknown keys are ignored and "app"/"profile" may hold
        new names. Keys that decide how FRAME bytes are read must be known values.
        """
        try:
            obj = json.loads(json_bytes.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                             parse_constant=_no_nan)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ProtocolError(f"invalid schema JSON: {exc}") from exc
        if type(obj) is not dict:
            raise ProtocolError("schema JSON must be an object")
        if obj.get("schema_version") != 1 or type(obj.get("schema_version")) is not int:
            raise ProtocolError("schema_version must be 1")
        for key, expected in _FIXED_SCHEMA_VALUES.items():
            if obj.get(key) != expected:
                raise ProtocolError(f"{key} must be {expected!r}")
        encoding = obj.get("blend_encoding")
        if encoding not in ("i16", "i32"):
            raise ProtocolError("blend_encoding must be 'i16' or 'i32'")
        app, profile = obj.get("app", ""), obj.get("profile", "")
        if type(app) is not str or type(profile) is not str:
            raise ProtocolError("app and profile must be strings")
        names = obj.get("blend_names")
        if type(names) is not list or len(names) > MAX_BLENDSHAPES:
            raise ProtocolError("blend_names must be a list of at most 4096 names")
        for name in names:
            if type(name) is not str:
                raise ProtocolError("blend names must be strings")
            try:
                size = len(name.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise ProtocolError("blend name is not valid Unicode") from exc
            if not 1 <= size <= MAX_NAME_BYTES or any(ord(c) < 0x20 for c in name):
                raise ProtocolError("blend name must be 1-255 UTF-8 bytes without control characters")
        if len(set(names)) != len(names):
            raise ProtocolError("duplicate blend name")
        layout = struct.Struct(f"<{len(names)}{'h' if encoding == 'i16' else 'i'}")
        return cls(session_id, schema_id, app, profile, tuple(names), encoding,
                   MappingProxyType({name: i for i, name in enumerate(names)}), layout)


APP_PROFILES = MappingProxyType({"iFacialMocap": "ifacialmocap-stream",
                                 "Facemotion3d": "facemotion3d-other"})


def schema_json(names: Sequence[str], *, app: str = "iFacialMocap", encoding: str = "i16") -> bytes:
    """Build schema JSON the way the iOS apps do (used by the simulator and tests)."""
    obj = {"schema_version": 1, "app": app, "profile": APP_PROFILES.get(app, ""),
           "blend_names": list(names), "blend_encoding": encoding, "blend_unit": "percent",
           "pose_layout": POSE_LAYOUT, "rotation_unit": "degree", "position_unit": "meter"}
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    Schema.parse(data)
    return data


def schema_body(json_bytes: bytes, start: StartInfo) -> bytes:
    """Complete SCHEMA payload: 20-byte start information followed by the JSON."""
    return start.pack() + json_bytes


@dataclass(frozen=True)
class Frame:
    """One decoded FRAME. BlendShape values are integer percent: -25 means -0.25."""
    schema: Schema
    sequence: int
    source_token: int      # app-defined UInt32; 0 = not available. Receivers may ignore it.
    tracking: bool         # flags bit 0: face tracked (live: now; playback: when this frame was recorded)
    playback: bool         # flags bit 1: the values come from a recording being played back
    blend_values: tuple[int, ...]
    pose: tuple[float, ...]
    received_monotonic: float

    @property
    def head(self) -> tuple[float, ...]:
        """rx, ry, rz in degrees; px, py, pz in meters (after the app's scaling)."""
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
        return {"app": self.schema.app, "session_id": self.schema.session_id,
                "schema_id": self.schema.schema_id, "sequence": self.sequence,
                "source_token": self.source_token, "tracking": self.tracking,
                "playback": self.playback,
                "blend_shapes": dict(zip(self.schema.blend_names, self.blend_values)),
                "head": self.head, "right_eye": self.right_eye, "left_eye": self.left_eye}


def frame_payload(schema: Schema, values: Sequence[int], pose: Sequence[float]) -> bytes:
    """FRAME payload for `schema` (used by the simulator and tests)."""
    if len(values) != len(schema.blend_names) or len(pose) != 12:
        raise ProtocolError("value count does not match the schema")
    if any(type(v) is not int for v in values):
        raise ProtocolError("BlendShape values must be integers")
    try:
        data = schema.blend_struct.pack(*values) + POSE.pack(*pose)
    except (struct.error, OverflowError) as exc:
        raise ProtocolError(f"value does not fit {schema.blend_encoding}: {exc}") from exc
    if not all(math.isfinite(v) for v in POSE.unpack_from(data, schema.blend_struct.size)):
        raise ProtocolError("pose values must be finite Float32")
    return data


# =============================================================================
# 4. One accepted session (pure logic, no sockets)
# =============================================================================

class Session:
    """State of one iOS session, created from its first complete, valid SCHEMA.

    Returns decoded Schema/Frame objects and queues the replies the network client
    must send (`outbound`: (message type, schema_id)). UDP acknowledges every newly
    stored schema; TCP never sends SCHEMA_ACK.
    """

    def __init__(self, session_id: int, start: StartInfo, schema: Schema, body: bytes, *,
                 transport: str, now: float) -> None:
        self.session_id = session_id
        self.start = start
        self.schema = schema
        self.transport = transport
        self.last_valid_receive = now
        self.outbound: list[tuple[int, int]] = []
        self.stats = {"frames": 0, "stale_frames": 0, "unknown_schema": 0, "sequence_gaps": 0,
                      "schema_changes": 1}
        self._body = body                  # complete SCHEMA payload of the current schema
        self._assembly = Reassembler()
        self._last_sequence: int | None = None
        self._last_ack = self._last_request = -math.inf
        self.ack_current(now, force=True)

    @property
    def udp_size(self) -> int:
        return self.start.max_udp_size

    @property
    def lease_seconds(self) -> float:
        return self.start.lease_ms / 1000.0

    def ack_current(self, now: float, *, force: bool = False) -> None:
        if self.transport == "udp" and (force or now - self._last_ack >= CONTROL_MIN_INTERVAL):
            self.outbound.append((SCHEMA_ACK, self.schema.schema_id))
            self._last_ack = now

    def request_schema(self, schema_id: int, now: float) -> None:
        if now - self._last_request >= CONTROL_MIN_INTERVAL:
            self.outbound.append((GET_SCHEMA, schema_id))
            self._last_request = now

    def expire(self, now: float) -> None:
        self._assembly.expire(now)

    def receive(self, packet: Packet, now: float) -> Schema | Frame | None:
        if packet.kind == FRAME:
            return self._receive_frame(packet, now)
        if packet.kind == SCHEMA:
            return self._receive_schema(packet, now)
        if packet.kind == PONG:
            self.last_valid_receive = now
            return None
        if packet.kind == ERROR:
            raise RemoteError(packet.payload.decode("utf-8"))
        raise ProtocolError(f"{TYPE_NAMES[packet.kind]} is never sent by iOS")

    def _receive_schema(self, packet: Packet, now: float) -> Schema | None:
        current = self.schema.schema_id
        if packet.schema_id < current:
            return None                                   # an old table never rolls back
        if packet.part_index == 0 and StartInfo.parse(packet.payload) != self.start:
            raise ProtocolError("session settings changed without a new session")
        if packet.schema_id == current:
            # Already stored completely: one matching piece is enough to re-ACK.
            offset = packet.part_index * (SCHEMA_DATAGRAM_SIZE - HEADER_SIZE)
            if (packet.total_length != len(self._body)
                    or packet.payload != self._body[offset:offset + len(packet.payload)]):
                raise ProtocolError("schema_id reused with different contents")
            self.last_valid_receive = now
            self.ack_current(now)
            return None
        body = self._assembly.push(packet, now)
        if body is None:
            return None
        if StartInfo.parse(body) != self.start:
            raise ProtocolError("session settings changed without a new session")
        schema = Schema.parse(body[START_INFO.size:], self.session_id, packet.schema_id)
        # Commit only after the whole table is valid; old partial data is dropped.
        self._assembly.clear()
        self.outbound = [item for item in self.outbound if item[0] not in (SCHEMA_ACK, GET_SCHEMA)]
        self.schema, self._body = schema, body
        self.last_valid_receive = now
        self.stats["schema_changes"] += 1
        self.ack_current(now, force=True)
        return schema

    def _receive_frame(self, packet: Packet, now: float) -> Frame | None:
        schema = self.schema
        if packet.schema_id > schema.schema_id:
            self.stats["unknown_schema"] += 1
            self.request_schema(packet.schema_id, now)
            return None
        if packet.schema_id < schema.schema_id or (
                self._last_sequence is not None and not is_newer_u32(packet.sequence, self._last_sequence)):
            self.stats["stale_frames"] += 1
            return None
        if packet.total_length != schema.frame_bytes:
            raise ProtocolError("FRAME length does not match the schema")
        data = self._assembly.push(packet, now)
        if data is None:
            return None
        values = schema.blend_struct.unpack_from(data)
        pose = POSE.unpack_from(data, schema.blend_struct.size)
        if not all(math.isfinite(v) for v in pose):
            raise ProtocolError("FRAME pose contains NaN or infinity")
        if self._last_sequence is not None:
            self.stats["sequence_gaps"] += ((packet.sequence - self._last_sequence) & 0xFFFFFFFF) - 1
        self._last_sequence = packet.sequence
        self.last_valid_receive = now
        self.stats["frames"] += 1
        return Frame(schema, packet.sequence, packet.source_token, bool(packet.flags & TRACKED),
                     bool(packet.flags & PLAYBACK), values, pose, now)


@dataclass
class _Candidate:
    """A new session whose first SCHEMA is still arriving. Only one exists at a time."""
    peer: object
    session_id: int
    created: float
    connection: object = None
    assembly: Reassembler = field(default_factory=Reassembler)


# =============================================================================
# 5. Network client
# =============================================================================

@dataclass(frozen=True)
class RecoveryPolicy:
    """Receiver timers in seconds (PROTOCOL_V3.md section 6 lists the defaults).

    Only a new, valid FRAME resets the FRAME clock; SCHEMA and PONG do not.
    Nothing here closes the PC port: when retries end the receiver keeps waiting.
    """
    hello_attempts: int = 5         # UDP HELLOs / TCP connection attempts at startup
    hello_interval: float = 1.0
    connect_timeout: float = 3.0     # TCP: one connection attempt
    first_schema_timeout: float = 5.0  # TCP: from connection established or accepted
    frame_timeout: float = 3.0       # no new FRAME -> RECOVERING
    repair_interval: float = 1.0
    repair_attempts: int = 3
    wait_after: float = 12.0         # no new FRAME -> WAITING
    ping_interval: float = 3.0

    def __post_init__(self) -> None:
        for name in ("hello_interval", "connect_timeout", "first_schema_timeout", "frame_timeout",
                     "repair_interval", "wait_after", "ping_interval"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        for name in ("hello_attempts", "repair_attempts"):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 20:
                raise ValueError(f"{name} must be 1-20")
        if self.wait_after <= self.frame_timeout + self.repair_interval * (self.repair_attempts - 1):
            raise ValueError("wait_after must be later than the last repair attempt")


class FrameWatchdog:
    """Receiver state: WAIT_SCHEMA -> WAIT_FRAME -> STREAMING <-> RECOVERING -> WAITING."""

    def __init__(self, policy: RecoveryPolicy, *, passive: bool = False) -> None:
        self.policy = policy
        self.state = "WAITING" if passive else "WAIT_SCHEMA"
        self.since: float | None = None      # first schema stored, then last new FRAME
        self.attempts = 0
        self.last_repair = -math.inf
        self._exhausted = False

    def schema_ready(self, now: float) -> None:
        if self.since is None:
            self.since = now
        if not self._exhausted:
            self.state = "WAIT_FRAME"

    def frame_received(self, now: float) -> None:
        self.since = now
        self.attempts = 0
        self.last_repair = -math.inf
        self.state = "STREAMING"
        self._exhausted = False

    def wait(self) -> None:
        self.state = "WAITING"
        self._exhausted = True

    def poll(self, now: float) -> str | None:
        """Return "repair", "wait" or None."""
        if self.state == "WAITING" or self.since is None:
            return None
        age = now - self.since
        if age >= self.policy.wait_after:
            self.wait()
            return "wait"
        if (age >= self.policy.frame_timeout and self.attempts < self.policy.repair_attempts
                and now - self.last_repair >= self.policy.repair_interval):
            self.attempts += 1
            self.last_repair = now
            self.state = "RECOVERING"
            return "repair"
        return None


@dataclass
class _Connection:
    """One TCP connection. A session is valid only on the connection that delivered its SCHEMA."""
    sock: socket.socket
    peer: tuple
    outgoing: bool
    started: float                     # connect() call or accept() time
    established: float | None = None   # None while an outgoing connect is in progress
    framer: TCPFramer = field(default_factory=TCPFramer)


def _call(callback: Callable, argument: object) -> None:
    try:
        callback(argument)
    except Exception as exc:
        raise CallbackError(f"{type(argument).__name__} callback failed: {exc}") from exc


class V3Client:
    """Receive FMV3 over UDP or TCP. Everything runs in the thread that calls run().

    host        iOS address for a normal start; also the only address accepted.
    listen_only True: send nothing, wait for a manual start from any LAN address.
    ios_port    port the PC connects to (default UDP 49983 / TCP 49984).
    pc_port     port this PC listens on (default UDP 49983 / TCP 49984; 0 = any, for tests).

    There is no authentication: use a trusted LAN. A running stream is never taken
    over; a new session is accepted only in WAIT_SCHEMA or WAITING.
    """

    def __init__(self, host: str | None = None, *, transport: str = "udp",
                 ios_port: int | None = None,
                 pc_port: int | None = None, bind: str = "0.0.0.0", listen_only: bool = False,
                 fps: int = 60, udp_size: int = MAX_UDP_SIZE,
                 recovery: RecoveryPolicy | None = None) -> None:
        ios_default, pc_default = default_ports(transport)
        self.ios_port = ios_default if ios_port is None else ios_port
        self.pc_port = pc_default if pc_port is None else pc_port
        if type(self.ios_port) is not int or not 1 <= self.ios_port <= 65535:
            raise ValueError("ios_port must be 1-65535")
        if type(self.pc_port) is not int or not 0 <= self.pc_port <= 65535:
            raise ValueError("pc_port must be 0-65535")
        hello_payload(fps, udp_size)  # validates fps and udp_size
        if not host and not listen_only:
            raise ValueError("give the iOS host, or listen_only=True to wait for a manual start")
        self.host, self.transport = host, transport
        self.bind, self.listen_only = bind, listen_only
        self.fps, self.udp_size = fps, udp_size
        self.policy = recovery or RecoveryPolicy()
        self.state = "STOPPED"
        self.last_error: str | None = None
        self.local_port: int | None = None
        self.session: Session | None = None
        self.watchdog = FrameWatchdog(self.policy, passive=listen_only)
        self._reset_runtime()

    # ---- runtime state --------------------------------------------------------

    def _reset_runtime(self) -> None:
        self.session = None
        self._session_peer = None               # UDP: iOS address of the session
        self._session_conn: _Connection | None = None  # TCP: connection of the session
        self._conn: _Connection | None = None   # TCP: the current connection
        self._sock: socket.socket | None = None  # UDP socket or TCP listener
        self._candidate: _Candidate | None = None
        self._retired: list[tuple] = []
        self._hello_nonce = secrets.randbits(64) or 1
        self._hello_pending = False
        self._target = None
        self._allowed_ips: set[str] = set()
        self._last_ping = -math.inf
        self._ping_sequence = 0
        self._on_state = None

    def _set_state(self, reason: str = "") -> None:
        if self.state != self.watchdog.state:
            self.state = self.watchdog.state
            LOG.info("State=%s%s", self.state, f": {reason}" if reason else "")
            if self._on_state:
                _call(self._on_state, self.state)

    def _enter_waiting(self, reason: str) -> None:
        self._hello_pending = False
        self.watchdog.wait()
        if self.session is not None:
            self.session.outbound.clear()
        self._set_state(reason)

    def _retire_session(self) -> None:
        if self.session is not None:
            peer = self._session_conn.peer if self._session_conn else self._session_peer
            self._retired = (self._retired + [(peer, self.session.session_id)])[-RETIRED_SESSIONS:]
        self.session, self._session_peer, self._session_conn = None, None, None

    # ---- sockets ---------------------------------------------------------------

    def _resolve(self) -> int:
        family = socket.AF_INET6 if ":" in self.bind else socket.AF_INET
        if self.host:
            kind = socket.SOCK_DGRAM if self.transport == "udp" else socket.SOCK_STREAM
            results = socket.getaddrinfo(self.host, self.ios_port, family, kind)
            self._allowed_ips = {item[4][0] for item in results}
            self._target = results[0][4]
        return family

    def _allowed(self, address) -> bool:
        return bool(address) and (not self._allowed_ips or address[0] in self._allowed_ips)

    def _open_pc_port(self, family: int) -> socket.socket:
        """Bind the single PC port. Another program on it is an error, never a fallback.

        SO_REUSEADDR is deliberately not set: on macOS it would let a second program
        bind the same port on a specific address with no error. Windows gets
        SO_EXCLUSIVEADDRUSE for the same reason.
        """
        sock = socket.socket(family, socket.SOCK_DGRAM if self.transport == "udp" else socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262_144)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((self.bind, self.pc_port))
            if self.transport == "tcp":
                sock.listen(4)
            sock.setblocking(False)
        except OSError as exc:
            sock.close()
            in_use = exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in (10048, 10013)
            if in_use:
                raise PortInUseError(
                    exc.errno, f"{self.transport.upper()} port {self.pc_port} on this PC is already in use. "
                    "Close the other program (another receiver or the simulator?) or choose a port "
                    "with --pc-port. After a TCP connection closes, the OS may keep the port busy "
                    "for up to a minute.") from exc
            raise
        self.local_port = sock.getsockname()[1]
        return sock

    def _send(self, kind: int, payload: bytes = b"", *, schema_id: int = 0, sequence: int = 0) -> None:
        if kind == HELLO:
            session_id, udp_size = self._hello_nonce, self.udp_size
            peer, conn = self._target, self._conn
        elif self.session is not None:
            session_id, udp_size = self.session.session_id, self.session.udp_size
            peer, conn = self._session_peer, self._session_conn
        else:
            return
        if self.transport == "udp":
            if peer is None or self._sock is None:
                return
            for datagram in encode_message(kind, payload, session_id=session_id, schema_id=schema_id,
                                           sequence=sequence, udp_size=udp_size):
                self._sock.sendto(datagram, peer)
        else:
            if conn is None or conn.established is None:
                return
            conn.sock.sendall(encode_message(kind, payload, session_id=session_id,
                                             schema_id=schema_id, sequence=sequence)[0])
        if kind == HELLO:
            self._hello_pending = True

    def _flush(self) -> None:
        """Send queued replies. While WAITING only SCHEMA_ACK is sent (no requests)."""
        if self.session is None:
            return
        queued, self.session.outbound = self.session.outbound, []
        for kind, schema_id in queued:
            if kind == SCHEMA_ACK or self.watchdog.state != "WAITING":
                self._send(kind, schema_id=schema_id)

    # ---- receiving -------------------------------------------------------------

    def _dispatch(self, packet: Packet, peer, conn: _Connection | None, now: float,
                  on_frame, on_schema) -> None:
        if not self._allowed(peer) or (peer, packet.session_id) in self._retired:
            return
        session = self.session
        on_session_link = (conn is self._session_conn) if self.transport == "tcp" else (peer == self._session_peer)
        if session is not None and on_session_link and packet.session_id == session.session_id:
            self._session_packet(packet, now, on_frame, on_schema)
        elif packet.kind == ERROR:
            self._hello_refusal(packet, peer, conn)
        elif packet.kind == SCHEMA:
            self._new_session_schema(packet, peer, conn, now, on_schema)
        # Anything else that does not belong to the current session is ignored.

    def _session_packet(self, packet: Packet, now: float, on_frame, on_schema) -> None:
        try:
            event = self.session.receive(packet, now)
        except RemoteError as exc:
            self.last_error = str(exc)
            LOG.warning("iOS ended the session: %r. The PC port stays open.", str(exc))
            self._enter_waiting("iOS ended the session")
            return
        was_waiting = self.watchdog.state == "WAITING"
        if isinstance(event, Schema):
            if on_schema:
                _call(on_schema, event)
            self.watchdog.schema_ready(now)
            LOG.info("Schema %d: %d BlendShapes, %s", event.schema_id, len(event.blend_names),
                     event.blend_encoding)
        elif isinstance(event, Frame):
            _call(on_frame, event)
            self.watchdog.frame_received(now)
            if was_waiting:
                self._last_ping = now
        self._flush()
        self._set_state()

    def _hello_refusal(self, packet: Packet, peer, conn: _Connection | None) -> None:
        """Apply an ERROR only to the HELLO that is still waiting for an answer."""
        if self.transport == "tcp":
            from_request = conn is not None and conn is self._conn and conn.outgoing
        else:
            from_request = peer == self._target
        if (self._hello_pending and self.session is None and self.watchdog.state == "WAIT_SCHEMA"
                and not self.listen_only and from_request and packet.session_id == self._hello_nonce):
            reason = packet.payload.decode("utf-8")
            self.last_error = reason
            hint = (" (the iOS app and this receiver use different FMV3 revisions)"
                    if reason.startswith("UNSUPPORTED_CONTRACT") else "")
            LOG.warning("iOS refused the request: %r%s. The PC port stays open.", reason, hint)
            self._enter_waiting("iOS refused the request")

    def _new_session_schema(self, packet: Packet, peer, conn: _Connection | None, now: float,
                            on_schema) -> None:
        if self.watchdog.state not in ("WAIT_SCHEMA", "WAITING"):
            return                                # never take over a running stream
        candidate = self._candidate
        if candidate is None or (candidate.peer, candidate.session_id, candidate.connection) != (
                peer, packet.session_id, conn):
            if candidate is not None and now - candidate.created < CANDIDATE_TIMEOUT:
                return                            # one incomplete candidate at a time
            candidate = self._candidate = _Candidate(peer, packet.session_id, now, conn)
        if packet.part_index == 0 and not self._acceptable_start(StartInfo.parse(packet.payload)):
            self._candidate = None
            return
        body = candidate.assembly.push(packet, now)
        if body is None:
            return
        self._candidate = None
        start = StartInfo.parse(body)
        if not self._acceptable_start(start):
            return
        schema = Schema.parse(body[START_INFO.size:], packet.session_id, packet.schema_id)
        if on_schema:
            _call(on_schema, schema)              # no ACK and no adoption if this fails
        self._retire_session()
        self.session = Session(packet.session_id, start, schema, body, transport=self.transport, now=now)
        self._session_peer, self._session_conn = peer, conn
        self._hello_pending = False
        self._last_ping = now
        self.watchdog.schema_ready(now)
        LOG.info("Session started (%s): schema %d, %d BlendShapes, %s, app=%s",
                 "manual" if start.client_nonce == 0 else "normal", schema.schema_id,
                 len(schema.blend_names), schema.blend_encoding, schema.app or "?")
        self._flush()
        self._set_state()

    def _acceptable_start(self, start: StartInfo) -> bool:
        """Normal start: nonce echoes our HELLO. Manual start: nonce 0.

        Only a normal start is held to our HELLO's fps and udp_size. A manual start
        had no HELLO, so iOS never saw them; StartInfo.parse already checked the
        valid ranges (1-60 fps, 576-1200 bytes).
        """
        if start.client_nonce not in (0, self._hello_nonce):
            return False
        if start.client_nonce != 0 and (start.actual_fps > self.fps
                                        or start.max_udp_size > self.udp_size):
            raise ProtocolError("SCHEMA settings exceed what this receiver requested")
        return True

    # ---- timers ----------------------------------------------------------------

    def _tick(self, now: float) -> None:
        if self._candidate is not None and now - self._candidate.created >= CANDIDATE_TIMEOUT:
            self._candidate = None
        session = self.session
        if session is not None:
            session.expire(now)
        action = self.watchdog.poll(now)
        if action == "wait":
            self._enter_waiting("no new FRAME; the PC port stays open for iOS")
            return
        if action == "repair" and session is not None:
            session.ack_current(now)              # UDP: in case our ACK was lost
            session.request_schema(0, now)        # and ask for the newest table
            self._flush()
            self._set_state(f"FRAME recovery {self.watchdog.attempts}/{self.policy.repair_attempts}")
        if session is None or self.watchdog.state == "WAITING":
            return
        if now - session.last_valid_receive >= session.lease_seconds:
            self._enter_waiting("nothing valid received within the session lease")
            return
        if now - self._last_ping >= self.policy.ping_interval:
            self._ping_sequence = (self._ping_sequence + 1) & 0xFFFFFFFF
            self._last_ping = now
            self._send(PING, sequence=self._ping_sequence)

    # ---- UDP ---------------------------------------------------------------------

    def _run_udp(self, stopped, on_frame, on_schema, start: float) -> None:
        attempts, next_hello, last_warning = 0, start, -math.inf
        while not stopped():
            now = time.monotonic()
            if self.watchdog.state == "WAIT_SCHEMA" and not self.listen_only and now >= next_hello:
                if attempts < self.policy.hello_attempts:
                    attempts += 1
                    next_hello = now + self.policy.hello_interval
                    try:
                        self._send(HELLO, hello_payload(self.fps, self.udp_size))
                    except OSError as exc:
                        self.last_error = str(exc)
                else:
                    self._enter_waiting("no SCHEMA after 5 HELLOs; waiting for iOS")
            try:
                self._tick(now)
            except OSError as exc:                # e.g. ICMP unreachable; keep the port
                self.last_error = str(exc)
            try:
                ready, _, _ = select.select([self._sock], [], [], 0.2 if self.state == "WAITING" else 0.05)
                if not ready:
                    continue
                data, peer = self._sock.recvfrom(65536)
                if not self._allowed(peer):
                    continue
                limit = self.session.udp_size if self.session and peer == self._session_peer else self.udp_size
                self._dispatch(decode_packet(data, max_udp_size=limit), peer, None, time.monotonic(),
                               on_frame, on_schema)
            except (ProtocolError, OSError) as exc:
                self.last_error = str(exc)
                if time.monotonic() - last_warning >= 1.0:
                    LOG.warning("Ignored UDP datagram: %s", exc)
                    last_warning = time.monotonic()

    # ---- TCP ---------------------------------------------------------------------

    def _close_connection(self, reason: str) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        conn.sock.close()
        self._hello_pending = False
        if self._candidate is not None and self._candidate.connection is conn:
            self._candidate = None
        if conn is self._session_conn:            # a session never moves to another connection
            self._retire_session()
            self._enter_waiting(f"{reason}; waiting for iOS on TCP {self.local_port}")

    def _connect(self, now: float, family: int) -> None:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        self._conn = _Connection(sock, self._target, outgoing=True, started=now)
        error = sock.connect_ex(self._target)
        if error not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, 10035):
            self._close_connection("TCP connection failed")

    def _connected(self, now: float) -> None:
        conn = self._conn
        error = conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if error:
            self._close_connection("TCP connection failed")
            return
        conn.established = now                    # the first-SCHEMA deadline starts here
        conn.sock.settimeout(0.5)
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            self._send(HELLO, hello_payload(self.fps, self.udp_size))
        except OSError as exc:
            self.last_error = str(exc)
            self._close_connection("HELLO could not be sent")

    def _accept(self, now: float) -> None:
        try:
            sock, address = self._sock.accept()
        except OSError:
            return
        if not self._allowed(address) or self.watchdog.state not in ("WAIT_SCHEMA", "WAITING"):
            sock.close()                          # a connection alone never replaces a stream
            return
        self._close_connection("replaced by a manual TCP connection from iOS")
        sock.settimeout(0.5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._conn = _Connection(sock, address, outgoing=False, started=now, established=now)

    def _run_tcp(self, stopped, on_frame, on_schema, start: float, family: int) -> None:
        attempts, next_connect = 0, start
        while not stopped():
            now = time.monotonic()
            if (self._conn is None and self.watchdog.state == "WAIT_SCHEMA" and not self.listen_only
                    and now >= next_connect):
                if attempts < self.policy.hello_attempts:
                    attempts += 1
                    next_connect = now + self.policy.hello_interval
                    self._connect(now, family)
                else:
                    self._enter_waiting(f"no connection after 5 attempts; waiting on TCP {self.local_port}")
            conn = self._conn
            if conn is not None:
                if conn.established is None:
                    if now - conn.started >= self.policy.connect_timeout:
                        self._close_connection("TCP connect timeout")
                elif conn is not self._session_conn and now - conn.established >= self.policy.first_schema_timeout:
                    self._close_connection("no complete SCHEMA within 5 s of connecting")
            try:
                self._tick(now)
            except OSError as exc:
                self.last_error = str(exc)
                self._close_connection("TCP send failed")
            conn = self._conn
            readers, writers = [self._sock], []
            if conn is not None:
                (readers if conn.established is not None else writers).append(conn.sock)
            ready, writable, _ = select.select(readers, writers, [], 0.2 if self.state == "WAITING" else 0.05)
            now = time.monotonic()
            if self._sock in ready:
                self._accept(now)
            conn = self._conn
            if conn is None:
                continue
            if conn.sock in writable and conn.established is None:
                self._connected(now)
            elif conn.sock in ready and conn.established is not None:
                try:
                    data = conn.sock.recv(65536)
                    if not data:
                        self._close_connection("TCP connection closed")
                        continue
                    for packet in conn.framer.feed(data):
                        self._dispatch(packet, conn.peer, conn, time.monotonic(), on_frame, on_schema)
                        if self._conn is not conn:
                            break
                except (ProtocolError, OSError) as exc:
                    self.last_error = str(exc)
                    LOG.warning("Closing TCP connection: %s", exc)
                    self._close_connection("invalid or broken TCP stream")

    # ---- public API --------------------------------------------------------------

    def run(self, on_frame: Callable[[Frame], None], *,
            on_schema: Callable[[Schema], None] | None = None,
            on_state: Callable[[str], None] | None = None,
            stop_event: threading.Event | None = None, duration: float | None = None) -> None:
        """Receive until stop_event is set or `duration` seconds pass.

        Silence from iOS never ends run(). A failing callback ends it with
        CallbackError; a failing on_schema is never acknowledged. PortInUseError
        is raised when the PC port is taken.
        """
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise ValueError("duration must be positive")
        stop_event = stop_event or threading.Event()
        start = time.monotonic()
        deadline = None if duration is None else start + duration

        def stopped() -> bool:
            return stop_event.is_set() or (deadline is not None and time.monotonic() >= deadline)

        self._reset_runtime()
        self._on_state = on_state
        self.watchdog = FrameWatchdog(self.policy, passive=self.listen_only)
        family = self._resolve()
        self._sock = self._open_pc_port(family)
        LOG.info("Listening on %s %s:%d (manual start target: this PC, this port)",
                 self.transport.upper(), self.bind, self.local_port)
        try:
            self._set_state()
            if self.transport == "udp":
                self._run_udp(stopped, on_frame, on_schema, start)
            else:
                self._run_tcp(stopped, on_frame, on_schema, start, family)
        finally:
            if self.session is not None:          # deliberate exit only; never for WAITING
                try:
                    self._send(STOP)
                except (OSError, ProtocolError):
                    pass
            if self._conn is not None:
                self._conn.sock.close()
            self._sock.close()
            self._conn = self._sock = None
            self.state = "STOPPED"


# =============================================================================
# 6. Console output and command line
# =============================================================================

def format_frame_log(frame: Frame, received_frames: int) -> str:
    """Every value of one frame on a single line (no trailing newline).

    Names are JSON-escaped so custom names cannot break the line. Read values from
    Frame in your own code; this text is for people.
    """
    blend_shapes = json.dumps(dict(zip(frame.schema.blend_names, frame.blend_values)),
                              ensure_ascii=True, separators=(",", ":"))
    return (f"frames={received_frames} seq={frame.sequence} tracked={frame.tracking} "
            f"count={len(frame.blend_values)} playback={frame.playback} app={frame.schema.app} "
            f"session_id={frame.schema.session_id} schema_id={frame.schema.schema_id} "
            f"source_token={frame.source_token} blend_encoding={frame.schema.blend_encoding} "
            f"blendShapes={blend_shapes} head={frame.head!r} rightEye={frame.right_eye!r} "
            f"leftEye={frame.left_eye!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="iPhone/iPad address (normal start); also the only accepted sender")
    parser.add_argument("--listen", action="store_true", help="wait for a manual start from iOS; send nothing first")
    parser.add_argument("--transport", choices=("udp", "tcp"), default="udp")
    parser.add_argument("--ios-port", type=int, help="port on iOS that this PC connects to (default UDP 49983 / TCP 49984)")
    parser.add_argument("--pc-port", type=int, help="port this PC listens on (default UDP 49983 / TCP 49984)")
    parser.add_argument("--bind", default="0.0.0.0", help="local address; use :: for IPv6")
    parser.add_argument("--fps", type=int, default=60,
                        help="highest frame rate to request in HELLO (1-60); a manual start uses the app's setting")
    parser.add_argument("--udp-size", type=int, default=MAX_UDP_SIZE,
                        help="largest FRAME datagram to request in HELLO (576-1200); a manual start uses the app's setting")
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    parser.add_argument("--jsonl", help="also write every frame to this new file (one JSON object per line)")
    parser.add_argument("--log-every", type=float, default=1.0,
                        help="seconds between printed frames; 0 = print none (every frame is still received)")
    args = parser.parse_args(argv)
    if not args.host and not args.listen:
        parser.error("give --host PHONE_IP, or --listen to wait for a manual start")
    if not math.isfinite(args.log_every) or args.log_every < 0:
        parser.error("--log-every must be 0 or more")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    output, frames, last_print = None, 0, -math.inf
    try:
        client = V3Client(args.host, transport=args.transport, ios_port=args.ios_port,
                          pc_port=args.pc_port, bind=args.bind, listen_only=args.listen,
                          fps=args.fps, udp_size=args.udp_size)
        if args.jsonl:
            output = open(args.jsonl, "x", encoding="utf-8", buffering=65536)

        def on_frame(frame: Frame) -> None:
            nonlocal frames, last_print
            frames += 1
            if output:
                output.write(json.dumps(frame.to_dict(), ensure_ascii=False) + "\n")
            now = time.monotonic()
            if args.log_every and now - last_print >= args.log_every:
                LOG.info("%s", format_frame_log(frame, frames))
                last_print = now

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
