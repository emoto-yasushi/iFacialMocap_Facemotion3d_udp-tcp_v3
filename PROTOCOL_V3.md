# Face Motion v3 — Communication protocol

[日本語](PROTOCOL_V3_JA.md) · [Run the Python sample](README.md)

This document is for developers building a receiver for iFacialMocap, iFacialMocapTr, or Facemotion3d. **Receiver** means the program on a PC or an embedded device. **iOS** means the app sending the motion data.

The exchange has two parts: iOS first sends a **SCHEMA**, which lists the field names and their order. It then sends **FRAME** messages containing numeric values in that order. UDP requires the receiver to acknowledge the schema before iOS sends frames; TCP does not.

| What you need | Read |
|---|---|
| Connect and start receiving | [2. Connection steps](#connections) |
| Build or decode a message | [3. Message formats](#messages) |
| Handle changed fields | [4. Schema changes](#schema-changes) |
| Handle missing data and reconnect | [5. Keeping communication running](#communication) |
| Check incoming data and resource limits | [6. Validation](#validation) |

**Protocol:** `contract_revision = 4`. This edition reorganizes the existing requirements; it does not change the wire format. Source specification: 2026-09-18; editorial update: 2026-09-23.

The connection overview is a starting point, not the entire implementation. Receivers also need the framing, validation, and recovery rules below. The reviewed Python snapshot has [known implementation differences](#known-issues); those are not alternative protocol rules.

<a id="overview"></a>

## 1. What the protocol carries

v3 carries BlendShape values and head/eye poses for live tracking and live playback of recordings. Every FRAME carries all values, including zeros. Frames are independent: there are no differences from an earlier frame to apply.

The schema can change. Its field order stays fixed until it is replaced, but neither the names nor the count are permanently fixed at 52. A session supports one receiver at a time and must not take over another active transfer.

Only the schema uses JSON. FRAME carries binary numbers, without field names, JSON, CSV, Base64, compression, or text delimiters. The application does not acknowledge or retransmit individual motion frames. TCP still performs its own transport-level acknowledgements, ordering, and retransmission.

Bulk recording transfer, FBX, audio files, body data, and software-specific bridges remain on their existing paths. The v3 change does not add, remove, or replace Bluetooth functionality. Instructions for preserving the existing iOS paths are in [Appendix A](#ios-notes).

<a id="supported-apps"></a>

### 1.1 Supported apps

| App | Minimum version |
|---|---|
| iFacialMocap | 1.5.3 |
| iFacialMocapTr | 1.2.6 |
| Facemotion3d | 1.4.6 |

These versions and later versions are supported; earlier versions are not. Facemotion3d requires its **Other license**. iFacialMocapTr uses the same connection defaults as iFacialMocap.

Set `contract_revision` to **4** in HELLO and in SCHEMA's start information. Reject other values; do not fall back automatically to another revision. App versions and `contract_revision` are different identifiers.

<a id="terms"></a>

### 1.2 Names and numbers used below

A **message** consists of a common header followed by its **payload** (the body). A **session** is one accepted stream between iOS and a receiver. A session can contain more than one schema if fields change.

| Field | Meaning |
|---|---|
| `message_type` | What the message is: HELLO, SCHEMA, FRAME, and so on. It is not a step number. |
| `session_id` | Identifies the stream. iOS generates it. The header uses the receiver's nonce instead for HELLO and an ERROR rejecting a pending HELLO. |
| `client_nonce` | A value the receiver generates so it can match a startup response to its HELLO. Manual startup uses 0 in SCHEMA's start information. |
| `schema_id` | Identifies a schema within the session. Increase it when the schema changes. |
| `sequence` | A frame number on FRAME, or a control sequence on PING/PONG. Its per-message rules are in Section 3.2. |
| `schema_version` | The version of the schema JSON format: **1**. |
| `contract_revision` | The version of this wire contract: **4**. |

“Sender address” means the other end's IP address and port. For TCP, also track the actual connection: a later connection using the same address is still a new connection.

<a id="connections"></a>

## 2. Connection steps

Choose normal startup when the receiver knows the iPhone/iPad's IP address. Choose manual startup when the user enters the receiver's IP address in the iOS app. These are different starting directions, not different message formats.

To run the supplied programs rather than implement a receiver, use the commands in [README.md](README.md).

<a id="ports"></a>

### 2.1 Standard ports

| App | iOS UDP listener | Receiver UDP listener | iOS direct TCP listener | Receiver manual TCP listener |
|---|---:|---:|---:|---:|
| iFacialMocap / iFacialMocapTr | 49983 | 49983 | 49984 | 49986 |
| Facemotion3d | 49993 | 49983 | 49994 | 49986 |

Normal TCP uses one receiver-initiated connection for traffic in both directions. Its source port may be assigned by the OS. Keep the receiver's **49986 listener open alongside it** for incoming manual connections; it is not a second connection used to deliver normal replies.

When the user chooses other ports, match the explicit settings at both ends. Do not overwrite saved settings. If a required local port is occupied, report the conflict rather than silently choosing another port or stopping the other process.

The receiver and iOS normally run on different devices, so using the same UDP port number is valid. When a simulator and receiver run on one computer, explicitly change the simulator's port to avoid a local bind conflict. See the README's simulator example.

<a id="udp-start"></a>

### 2.2 Normal UDP startup

Bind the receiver's UDP socket to its receive port, then send HELLO to the iOS UDP port. Use that same receiver socket for all subsequent messages.

```text
Receiver                                      iOS
   |---- HELLO (type 1) ----------------------->|
   |<--- SCHEMA (type 3) -----------------------|
   |     Validate and store the entire schema  |
   |---- SCHEMA_ACK (type 10) ----------------->|
   |<--- FRAME (type 4) ------------------------|
   |<--- FRAME (type 4) ------------------------|
```

iOS replies **from the socket that received HELLO**, to the receiver's actual source IP address and port. SCHEMA contains the startup response as well as the field mapping.

The receiver sends SCHEMA_ACK only after all schema fragments have arrived, the contents have been validated, and the field mapping has been stored. iOS sends no FRAME until it receives the correct ACK. The same rule applies after a schema change.

The numbers in this diagram are **message type IDs**. The UDP order is 1 → 3 → 10 → 4; each later FRAME still uses type 4. Missing SCHEMA/ACK messages are handled by [Section 5.3](#schema-retries).

<a id="tcp-start"></a>

### 2.3 Normal TCP startup

Connect to the iOS direct TCP port, then send HELLO on that connection. No UDP request is involved.

```text
Receiver                                      iOS
   |---- TCP connection ---------------------->|
   |---- HELLO (type 1) ----------------------->|
   |<--- SCHEMA (type 3) -----------------------|
   |<--- FRAME (type 4) ------------------------|
   |<--- FRAME (type 4) ------------------------|
```

iOS queues SCHEMA before FRAME. The receiver must validate and store SCHEMA before decoding the following frames. **Do not send an application-layer SCHEMA_ACK on TCP.** A separate GET_SCHEMA at normal startup and periodic schema retransmissions are unnecessary.

A TCP read may contain part of a message or several messages. Parse the stream using [Section 3.9](#tcp-framing), not the boundaries of individual reads. If the receiver cannot store the schema, it must not decode frames and must close the connection with an error.

<a id="manual-start"></a>

### 2.4 Manual startup from iOS

Start the receiver in listening mode first. In the iOS app, enter the receiver's IP address and receive port, then start sending. The receiver does not send HELLO for this startup.

| Transport | Exchange |
|---|---|
| UDP | iOS sends SCHEMA to the chosen receiver port → the receiver validates/stores it and sends SCHEMA_ACK → iOS starts FRAME. |
| TCP | iOS connects to the receiver's TCP listener → iOS sends SCHEMA followed by FRAME on that connection. No SCHEMA_ACK. |

For UDP, iOS waits for ACK on the same socket it used to send SCHEMA. For TCP, both sides use the accepted connection for further messages.

The manual-start SCHEMA has **`client_nonce = 0` in its start information**, a new nonzero `session_id` in its header, and an initial `schema_id` of 1. Each press of the manual-start button starts a new session; it must not take over another active client or a legacy transfer.

iOS selects `actual_fps` in 1–60 and `max_udp_size` in 576–1200; `lease_ms` defaults to 10000 and `contract_revision` is 4. The receiver rejects values above its own permitted FPS/UDP limits, which default to 60 and 1200. The nonce stays 0 throughout that session, including schema updates. Only a receiver configured to allow manual startup accepts this nonce value.

Manual startup uses the same purchase, time-limit, and foreground checks as normal startup. The UDP ACK-wait limit remains 10 seconds. If it expires, iOS ends that attempt and tells the user that receipt could not be confirmed; the receiver keeps listening. Retries, re-ACKs, and GET_SCHEMA must not reset trial/purchase timing or frame numbers.

<a id="messages"></a>

## 3. Message formats

Every message starts with the same **40-byte header**. The remaining bytes are its payload. Sizes below are byte counts; offsets start at 0. Send the specified binary bytes, not text describing those bytes.

The layouts in Sections 3.2–3.7 describe complete, unfragmented messages. For UDP fragmentation, use Section 3.8. For boundaries within a TCP stream, use Section 3.9.

<a id="message-type-ids"></a>

### 3.1 Message type IDs

Write one of the following IDs into `message_type`. The ID identifies the message; it does not count the steps in the exchange. Assign these values explicitly rather than numbering them from list positions.

| Decimal | Hex | Message | Direction | Payload |
|---:|---|---|---|---|
| 1 | `0x01` | HELLO | Receiver → iOS | 8-byte start request |
| 2 | `0x02` | **Reserved: unused and prohibited** | — | Not a valid message |
| 3 | `0x03` | SCHEMA | iOS → receiver | 20-byte start information + schema JSON |
| 4 | `0x04` | FRAME | iOS → receiver | BlendShape integers + 12 Float32 pose values |
| 5 | `0x05` | GET_SCHEMA | Receiver → iOS | Empty |
| 6 | `0x06` | PING | Receiver → iOS | Empty |
| 7 | `0x07` | PONG | iOS → receiver | Empty |
| 8 | `0x08` | STOP | Receiver → iOS | Empty |
| 9 | `0x09` | ERROR | iOS → receiver | UTF-8 reason, 1–512 bytes |
| 10 | `0x0A` | **SCHEMA_ACK** | Receiver → iOS | Empty; required on UDP |

**SCHEMA_ACK is 10 (`0x0A`), not 2 (`0x02`).** Decimal 10 and hexadecimal `0x0A` are the same number. `0x10` is decimal 16 and is not a valid type here.

“Reserved” means that type 2 has no permitted use in this contract. Do not send it, accept it as an ACK, or assign your own meaning to it. All other unlisted types are also invalid. Apply the [invalid-data rules](#invalid-data).

This restriction applies only to `message_type=2`. It does not prohibit 2 in a `schema_id` or numeric value that permits it, and it does not add padding or a missing step. The short history of this unused ID is in [Appendix D.1](#history).

<a id="common-header"></a>

### 3.2 Common header: 40 bytes

All multi-byte integers and Float32 values use **little-endian byte order with no padding**. Python's header format is `struct.Struct("<4sBBHQIIIIHHI")`. Write the fields in order; do not send the native in-memory layout of a Swift struct.

| Offset | Size | Type | Field | Meaning |
|---:|---:|---|---|---|
| 0 | 4 | bytes | `magic` | ASCII `FMV3`, bytes `46 4d 56 33` |
| 4 | 1 | UInt8 | `message_type` | ID from Section 3.1 |
| 5 | 1 | UInt8 | `flags` | FRAME flags; 0 on other messages |
| 6 | 2 | UInt16 | `header_size` | Always 40 |
| 8 | 8 | UInt64 | `session_id` | Nonzero iOS session ID; the HELLO/ERROR exception is below |
| 16 | 4 | UInt32 | `schema_id` | Schema number, as specified below |
| 20 | 4 | UInt32 | `sequence` | Frame or control sequence, as specified below |
| 24 | 4 | UInt32 | `source_token` | Used only on FRAME; 0 on all other messages |
| 28 | 4 | UInt32 | `total_payload_length` | Complete payload size before fragmentation, excluding the header |
| 32 | 2 | UInt16 | `part_index` | Fragment index, starting at 0 |
| 34 | 2 | UInt16 | `part_count` | Number of fragments; 1 when unfragmented |
| 36 | 4 | UInt32 | `chunk_length` | Payload bytes in this packet |
| 40 | variable | bytes | Payload | Complete payload or one fragment |

**Which value goes into `session_id`?** HELLO and an ERROR rejecting a still-pending HELLO use that HELLO's `client_nonce`. All other messages use the iOS-generated session ID. The header field is never 0. Manual startup's zero nonce belongs in SCHEMA's payload, not here.

| Header field | Per-message rule |
|---|---|
| `flags` | On FRAME, bit 0 indicates tracking and bit 1 indicates recording playback. All other bits are 0. The entire field is 0 on other message types. |
| `schema_id` | Nonzero on SCHEMA, FRAME, and SCHEMA_ACK. On GET_SCHEMA, use a requested schema ID or 0 for the latest. Use 0 on other types. |
| `sequence` | FRAME uses its frame sequence; PING/PONG use the matching control sequence. ERROR may carry a sequence, with 0 recommended. Use 0 on all other types, including retransmitted SCHEMA. |
| `source_token` | Only FRAME uses it. SCHEMA and all controls use 0. See Section 6.5. |

For an unfragmented message, set `part_index=0`, `part_count=1`, and `chunk_length=total_payload_length`. Empty payloads still require the full 40-byte header.

<a id="hello"></a>

### 3.3 HELLO: request a stream

**Direction:** receiver → iOS. **Type:** 1 (`0x01`). **Size:** 40-byte header + 8-byte payload = **48 bytes**.

| Payload offset | Size / type | Field | Allowed value |
|---:|---|---|---|
| 0 | 2 / UInt16 | `requested_fps` | 1–60 |
| 2 | 2 / UInt16 | `max_udp_size` | 576–1200, including the 40-byte FMV3 header of a FRAME datagram |
| 4 | 4 / UInt32 | `contract_revision` | 4 |

The payload format is `<HHI`. For 60 fps and a 1200-byte limit, its bytes are:

```text
3c 00 | b0 04 | 04 00 00 00
```

The separators are for reading only. `golden_vectors.json` contains a full 48-byte example under `hello_hex`.

Generate a random nonzero UInt64 `client_nonce` for this startup attempt sequence. Put it in the header's `session_id` field. Keep the same nonce when retransmitting the same HELLO.

```text
magic = FMV3              message_type = 1        header_size = 40
session_id = client_nonce
schema_id = 0            sequence = 0            source_token = 0
flags = 0                part_index = 0          part_count = 1
total_payload_length = 8                         chunk_length = 8
```

iOS checks whether the request is allowed, including purchase/usage conditions and conflicts with active transfers. It returns SCHEMA when accepted, or only ERROR when rejected. A matching rejection makes the receiver display the reason and enter WAITING without closing its listener.

If the first AR update is needed to prepare the schema, iOS sends SCHEMA as soon as that update is available, without waiting for another request. If preparation cannot finish within 5 seconds, it returns a short ERROR and releases the pending startup state.

An identical HELLO from the same source/TCP connection, with the same nonce and contents, returns the same session ID and current schema. It must not reset schema/frame numbers, usage limits, an ACK-wait deadline, or an acknowledged state. Reject different contents with the same nonce, and do not replace an active session with another sender or nonce. The UDP schema-send limit in Section 5.3 also applies to responses to repeated HELLOs.

<a id="schema"></a>

### 3.4 SCHEMA: define how to read the values

**Direction:** iOS → receiver. **Type:** 3 (`0x03`).

```text
[Common header: 40 bytes] [Start information: 20 bytes] [Schema JSON: J bytes]
Unfragmented total: 60 + J bytes
```

iOS generates a random nonzero UInt64 session ID for each new stream. The SCHEMA header carries that `session_id` and the current `schema_id` (1 or higher). Its `flags`, `sequence`, and `source_token` are 0. Initial, updated, and retransmitted schemas all use this same layout.

#### Start information

The first 20 payload bytes use `<QHHII`. JSON starts at payload offset 20, or message offset 60 when unfragmented.

| Payload offset | Size / type | Field | Meaning |
|---:|---|---|---|
| 0 | 8 / UInt64 | `client_nonce` | Echo HELLO's nonzero nonce for normal startup; 0 for manual startup |
| 8 | 2 / UInt16 | `actual_fps` | Agreed upper bound: 1 through `requested_fps` for normal startup |
| 10 | 2 / UInt16 | `max_udp_size` | Agreed FRAME datagram limit: 576 through the requested limit |
| 12 | 4 / UInt32 | `lease_ms` | Time allowed without valid receiver control messages, in milliseconds; default 10000, allowed 5000–60000 |
| 16 | 4 / UInt32 | `contract_revision` | 4 |

The nonce, FPS upper bound, UDP limit, lease, and revision stay unchanged within a session. The actual output rate may be below `actual_fps`. To renegotiate the upper bound, start a new HELLO/session. Manual-start values follow Section 2.4.

The receiver adopts the session and schema together only after validating the complete payload. Partial fragments and an early FRAME are not sufficient. Sender/nonce checks and session replacement rules are in [Section 6.2](#session-validation).

#### Schema JSON

Use uncompressed UTF-8 JSON with **exactly these nine keys**. This example defines four BlendShapes; it is not a mandatory field list.

```json
{
  "schema_version": 1,
  "app": "Facemotion3d",
  "profile": "facemotion3d-other",
  "blend_names": ["eyeBlinkLeft", "eyeBlinkRight", "jawOpen", "myCustomSmile"],
  "blend_encoding": "i16",
  "blend_unit": "percent",
  "pose_layout": "head_rxyz_pxyz_rightEye_rxyz_leftEye_rxyz",
  "rotation_unit": "degree",
  "position_unit": "meter"
}
```

| Key | Required meaning/value |
|---|---|
| `schema_version` | 1; the JSON format version |
| `app`, `profile` | `iFacialMocap` / `ifacialmocap-stream`, or `Facemotion3d` / `facemotion3d-other` |
| `blend_names` | The names in the exact order of the FRAME integer array |
| `blend_encoding` | `i16` or `i32`, one type for all BlendShapes in this schema |
| `blend_unit` | `percent` |
| `pose_layout` | `head_rxyz_pxyz_rightEye_rxyz_leftEye_rxyz` |
| `rotation_unit` | `degree` |
| `position_unit` | `meter` |

`schema_id` belongs in the header, not the JSON. The numeric-type key is `blend_encoding`, not `value_type`. Do not add keys for licensing or for iFacialMocapTr. The app/profile pairs above remain unchanged.

The receiver maps each name to an array position when the schema arrives. It then reads that position in each FRAME. Name, JSON, and size validation rules are in [Section 6.1](#limits).

<a id="schema-ack"></a>

### 3.5 SCHEMA_ACK: confirm that the schema is stored

**Direction:** receiver → iOS. **Type:** 10 (`0x0A`). **Transport:** UDP only. **Size: 40 bytes, with no payload.**

The receiver sends this message after validating and storing the entire schema. Any `on_schema` callback must finish successfully before the ACK is sent.

```text
magic = FMV3              message_type = 10       header_size = 40
session_id = session_id from the accepted SCHEMA
schema_id = schema_id of the validated, stored SCHEMA
flags = 0                sequence = 0            source_token = 0
total_payload_length = 0
part_index = 0           part_count = 1          chunk_length = 0
```

Byte offset 4, the fifth byte of the header, contains `0A`. Send the whole header, not that byte alone and not the strings `"0x0A"` or `"10"`. Do not substitute type 2 or place HELLO's nonce in the session ID field.

iOS accepts an ACK only if the sender's IP/port, session ID, and current schema ID all match, and iOS actually sent that schema. An ACK for an older or future schema does not allow FRAME transmission.

The ACK contains no repeated names or checksum. It relies on the complete SCHEMA payload being immutable for each session/schema ID pair. It confirms synchronization, not cryptographic identity. Re-ACKing a saved schema is described in Section 5.3; an ACK never needs another ACK in response.

<a id="frame"></a>

### 3.6 FRAME: read one set of motion values

**Direction:** iOS → receiver. **Type:** 4 (`0x04`). Read the integer array using the names and numeric type in the current schema.

```text
[Header: 40 bytes] [N BlendShape integers] [Head + right eye + left eye: 48 bytes]
                   i16: 2 × N bytes
                   i32: 4 × N bytes
```

Do not add a field count or numeric type to the payload: SCHEMA already provides them. Include every BlendShape value, including 0. `flags` and `source_token` belong in the header, not in this payload.

#### BlendShape values

With an i16 schema defining the following names, the integer portion is:

| Array position | Name | Value | Little-endian bytes |
|---:|---|---:|---|
| 0 | eyeBlinkLeft | 12 | `0c 00` |
| 1 | eyeBlinkRight | 8 | `08 00` |
| 2 | jawOpen | 45 | `2d 00` |
| 3 | myCustomSmile | -25 | `e7 ff` |

Values are signed integer percentage points. Divide by 100 when a consumer needs a coefficient: 25 → 0.25, -25 → -0.25, and 150 → 1.5. The decoder must not restrict values to 0–100.

If a value no longer fits i16, iOS must send a new schema using i32 **before sending that value**. Do not automatically downgrade to i16 within the same session. If a value does not fit i32, report ERROR rather than silently clamping it or overflowing. The sender keeps the existing rounding/calculation result; see Appendix A.

#### Head and eyes

After the integers, read **12 Float32 values** in the order below. `rx/ry/rz` are rotations; `px/py/pz` are positions.

| Order | Object | Values | Bytes |
|---:|---|---|---:|
| 1 | Head | rx, ry, rz, px, py, pz | 24 |
| 2 | Right eye | rx, ry, rz | 12 |
| 3 | Left eye | rx, ry, rz | 12 |

Rotations are in degrees. Positions are meter-derived values after the app's scaling, axis conversion, calibration, and mirroring. They are not absolute real-world coordinates. Preserve the profile's meaning rather than silently changing to another common coordinate system. All 12 values must be finite; reject NaN, infinity, and payload-length mismatches. Internal values such as `head[6...8]` are not transmitted.

When tracking is lost, clear flags bit 0. The last values may be retained, but they must not be marked as currently tracked. Set bit 1 during live playback. If a recording lacks tracking information, bit 0 reflects the current camera tracking state, not whether the playback data is valid.

Each session has its own UInt32 FRAME sequence. Reject duplicate or older frames using [Section 6.3](#sequence). Do not reset the sequence when the schema changes.

For 52 BlendShapes encoded as i16, an unfragmented FRAME is **40 + 104 + 48 = 192 bytes**. At 60 frames/second, FRAME messages alone use **11520 bytes/second**. Network headers, schemas, and control messages are additional.

<a id="controls"></a>

### 3.7 Other control messages

GET_SCHEMA, PING, PONG, and STOP contain only the common 40-byte header. Their payload lengths are 0, `part_index=0`, and `part_count=1`. Apply the remaining header rules in Section 3.2. Controls from the receiver other than HELLO use the accepted iOS session ID; iOS checks the sender address or TCP connection as well.

| Message | What to send and what happens next |
|---|---|
| GET_SCHEMA (5) | Put the requested schema ID in `schema_id`, or 0 for the latest. iOS returns that schema, or the latest if it no longer holds the requested one. This does not replace SCHEMA_ACK. |
| PING (6) | The receiver sends a control sequence; iOS replies with PONG (7) carrying the same sequence. The interval and lease rules are in Section 5.7. |
| STOP (8) | The receiver explicitly ends the matching v3 session. It must not stop unrelated or legacy transfers. Running out of recovery attempts is not a reason to send STOP. |

Before any session has been accepted, repeat the same HELLO instead of GET_SCHEMA. Do not request retransmission of an old FRAME.

#### ERROR (9)

ERROR is sent **from iOS to the receiver**. Its payload is a valid UTF-8 reason, **1–512 bytes**, without an added terminator. Its schema ID, flags, and source token are 0; sequence 0 is recommended. For an unfragmented ERROR, set `part_index=0`, `part_count=1`, and both payload-length fields to the reason's byte length.

| When iOS sends ERROR | Value in the header's `session_id` field |
|---|---|
| Rejecting a HELLO that has not been accepted | That HELLO's `client_nonce` |
| Reporting an error in an accepted session | The iOS-generated `session_id` |

Apply a HELLO rejection only when its nonce and expected sender/TCP connection match a **still-pending** request. Once a session has been adopted, a delayed rejection of the earlier HELLO must not change that session's state.

For a valid, matching ERROR, display the reason and enter WAITING without terminating the receiver. Validate UTF-8 and the sender/session before applying it. Malformed ERRORs follow Section 6.4. The reviewed receiver has a [known UTF-8 validation difference](#known-issues).

<a id="udp-framing"></a>

### 3.8 Splitting and reassembling UDP messages

SCHEMA may be larger than one UDP datagram. Split its payload at a fixed capacity of **536 bytes**, so each datagram contains at most **576 bytes including its 40-byte FMV3 header**. This rule is fixed before negotiation because the receiver has not yet read SCHEMA's start information.

FRAME uses the negotiated `max_udp_size` instead. For either message:

```text
SCHEMA: capacity = 576 - 40 = 536
FRAME : capacity = max_udp_size - 40
part_count = max(1, ceil(total_payload_length / capacity))
```

Each datagram contains a header followed by one payload fragment. Every fragment except the last has `capacity` payload bytes; the last has the remainder. There may be at most **512 fragments**. Do not invent separate start, continuation, or end message types.

Within one message, all fragments share `message_type`, `flags`, `session_id`, `schema_id`, `sequence`, `source_token`, `total_payload_length`, and `part_count`. `part_index` selects where each fragment belongs. Concatenate payload fragments in that order, then parse the resulting complete payload. SCHEMA's 20-byte start information occurs only once, at the start of that logical payload, not in every fragment.

Ignore identical duplicate fragments. If fragment contents or metadata conflict, discard the assembly. Fragments from retransmissions of the same schema and same payload may be reused. Never forward an incomplete FRAME to rendering. Buffer and assembly-time limits are in Section 6.1.

Keep the sender's queues bounded: one active schema-send operation, at most one pending resend, and finite batches. Prefer the latest motion over queued old motion. Large field counts may call for a lower UDP frame rate or TCP.

The fixed 576-byte limit does not guarantee delivery on every network path or a particular path MTU. This is a trusted-LAN protocol; use TCP or another appropriate transport when a special path cannot deliver these datagrams.

<a id="tcp-framing"></a>

### 3.9 Finding message boundaries in TCP

TCP provides a byte stream, not one message per read. Keep incoming bytes in a buffer and repeat these steps:

1. Wait until at least 40 bytes are available, then validate the common header.
2. Wait for the following `chunk_length` payload bytes. Header and payload together form one message.
3. Process that complete message and keep any remaining bytes for the next message.

On TCP, every message is unfragmented at the application level: `part_index=0`, `part_count=1`, and `chunk_length=total_payload_length`. Do not add a separate four-byte length prefix, a legacy terminator string, or schema-fragment messages.

Serialize writes on each connection. Replace motion that has not started sending with the latest single frame, while preserving the order of a required SCHEMA and its dependent FRAMEs. Do not truncate a message once sending has begun. Limit simultaneous writes and the receive buffer; a stalled network operation must not wait indefinitely.

Discard partial messages when a connection closes. Startup deadlines and the requirement to validate SCHEMA again on a new connection are in [Section 5.6](#tcp-reconnect).

<a id="schema-changes"></a>

## 4. When the schema changes

A value change does not by itself change the schema. Use a new schema ID when the interpretation of the numeric array changes.

| Change | New schema? |
|---|---|
| A value changes, tracking is lost, or audio becomes active/inactive | No; reflect it in FRAME |
| A field is added, removed, renamed, or reordered | Yes; increase `schema_id` |
| BlendShape encoding changes from i16 to i32 | Yes; increase `schema_id` |
| Only `source_token` changes | No; update the next FRAME header |

UDP pauses FRAME transmission until the new schema is acknowledged. TCP sends the new schema before frames using it, without waiting for SCHEMA_ACK. A given session/schema ID pair always denotes the same complete SCHEMA payload. If schema IDs run out, start a new session rather than reuse an ID with different contents.

<a id="schema-change-sender"></a>

### 4.1 What iOS does

For UDP, use **pause → send the new schema → receive its ACK → resume with the latest values**.

```text
iOS → receiver    FRAME using schema 7
                  iOS commits a change and pauses FRAME
iOS → receiver    SCHEMA 8
receiver → iOS    SCHEMA_ACK 8, after validation/storage
iOS → receiver    FRAME using schema 8
```

When settings are committed, iOS creates a fixed snapshot of the new schema, numeric type, and field indices. Discard old-schema frames/fragments not yet handed to the OS; already-sent packets cannot be recalled. Cache the new schema, send it, and enter `WAIT_SCHEMA_ACK`.

Resume only for the correct current-schema ACK from the same sender and session. Keep face processing and the display running while waiting, but retain only the latest numeric snapshot. After ACK, send the latest values, not a backlog. Do not reset the FRAME sequence.

If schema 9 is needed before schema 8 is acknowledged, replace the pending schema with 9 and wait only for its ACK. Ignore delayed ACKs for 8 and never mix arrays from the two layouts. Coalesce rapid changes to the latest schema, respecting the one-batch-per-second send limit. Further changes, retries, HELLOs, and PINGs do not extend the original 10-second ACK deadline for that uninterrupted wait.

On TCP, write already-sent old data → new SCHEMA → new FRAMEs on the same connection. No ACK-wait state is used.

<a id="schema-change-receiver"></a>

### 4.2 What the receiver does

Validate all fragments, the start information, and the JSON before replacing the current schema. Invalid input must not overwrite a valid schema. Update the stored indices and type, then complete `on_schema`; only afterward send the UDP ACK.

Once the new schema is committed, discard incomplete old-schema FRAMEs and queued old-schema ACKs. Do not decode late FRAMEs with an older schema ID, and never reinterpret an old numeric array with the new indices.

While still assembling the new schema, a late old FRAME may be decoded using the still-valid old schema. The two mappings must remain separate. The renderer may keep the last already-decoded pose after a schema change; this is different from reusing its old numeric array.

<a id="communication"></a>

## 5. Keeping communication running

There are two different jobs: **iOS waits for confirmation of a schema**, while **the receiver checks that new motion frames keep arriving**. Each side has its own timers. Reaching an iOS send deadline ends that send attempt; it does not require the receiver program to exit.

The following sections cover startup retries, missing schemas or ACKs, missing frames, and a new TCP connection after disconnection.

<a id="states"></a>

### 5.1 Receiver states

| State | Meaning |
|---|---|
| `WAIT_SCHEMA` | Waiting for the initial schema in normal startup. |
| `WAIT_FRAME` | The initial schema is ready; waiting for the first valid frame. |
| `STREAMING` | Receiving valid, new frames. |
| `RECOVERING` | Frames have stopped; making a limited number of recovery attempts. |
| `WAITING` | Automatic attempts have stopped, but incoming data and manual startup can still be accepted. |

`WAITING` does **not** mean that the computer or receiver program has shut down. It keeps the receiving socket/listener open. This is also different from iOS's `WAIT_SCHEMA_ACK` state, in which the sender waits for the receiver's acknowledgement.

<a id="startup-retries"></a>

### 5.2 When startup does not complete

| Receiver operation | Default retry rule | When the attempt limit is reached |
|---|---|---|
| UDP: no complete initial SCHEMA | Send HELLO immediately, then once per second, at most **5 sends total**. Repeat the same nonce and contents. | Enter `WAITING` and keep the socket open. |
| TCP: establish a normal outgoing connection | At most **5 connection attempts**, a default **3-second connect timeout**, and at least **1 second between attempts**. | Keep receive waiting rather than retrying forever. |

A valid ERROR for the current pending HELLO also moves the receiver to `WAITING`, with its reason displayed. Do not respond to an incomplete first schema with GET_SCHEMA: no session has been adopted yet, so the UDP retry is the same HELLO.

Startup also has separate **5-second deadlines**. Do not combine them into one timer:

| Who waits | What it waits for | Timing and action |
|---|---|---|
| iOS, normal TCP startup | A complete HELLO | From accepting the connection. Close that connection if HELLO is incomplete after 5 seconds. |
| iOS, preparing the startup response | Data needed to build SCHEMA | If the schema cannot be prepared within 5 seconds, send a short ERROR and release the pending startup state. |
| Receiver, on a new TCP connection | A complete initial SCHEMA | From establishing the connection, or accepting an incoming manual connection. After 5 seconds without a complete initial schema, close that connection but keep the listener. See [5.6](#tcp-reconnect). |

The schema may depend on the first AR update. iOS sends it as soon as it is available, without waiting for an additional request. These limits do not guarantee successful communication on an unavailable network.

<a id="schema-retries"></a>

### 5.3 When SCHEMA or SCHEMA_ACK is lost — UDP

Until iOS receives the correct ACK for its current schema, it sends the schema again instead of sending FRAMEs.

| Missing message | iOS does | Receiver does |
|---|---|---|
| All or part of the first SCHEMA | Keeps FRAME stopped and resends the same schema at 1-second intervals. | Does not ACK an incomplete schema; retries HELLO as in 5.2. |
| All or part of an updated SCHEMA | Keeps FRAME paused and resends the current schema. | Stores and ACKs only a complete, valid schema. Does not guess the new layout from the old one. |
| SCHEMA_ACK | Keeps FRAME stopped and resends the schema at 1-second intervals. | Recognizes the saved schema and sends its ACK again. |
| One FRAME | Does not retransmit that frame. | Uses the next fully received, valid frame. |

**iOS's ACK deadline is 10 seconds.** Start it when sending the initial schema, or when pausing FRAMEs for a schema change. If the current schema is still unacknowledged at that deadline, send `ERROR: SCHEMA_ACK_TIMEOUT` if possible and end that v3 session. The receiver continues listening.

SCHEMA sends triggered by HELLO, GET_SCHEMA, and a timer all share **one limit: at most one schema-send batch per second per peer**. A batch is all fragments of one schema transmission, not one fragment. With an initial send at time 0, the available send opportunities are 0, 1, …, 9 seconds; stop at 10 seconds. Do not overlap batches. This is not a combined limit on all control messages; HELLO, GET_SCHEMA, SCHEMA_ACK, and PING have their own rules.

Further schema changes, retransmissions, identical HELLOs, and PINGs do not restart the 10-second wait. Coalesce repeated changes to the latest schema. Continue processing PING/PONG and STOP during the wait. The session lease and existing usage-time limits remain independent: end the session when the earliest applicable deadline expires.

**On the receiver**, send the first ACK immediately after a new schema has been adopted. Subsequent re-ACKs are limited to once per second. If the complete schema is already stored, the receiver may compare a retransmitted fragment with the saved payload and re-ACK without receiving the whole retransmission. This shortcut is valid only because it already holds the complete, validated schema; it never permits ACKing an incomplete first receipt. Identical JSON does not need to be parsed again. There is no ACK of an ACK.

After confirmation, iOS may optionally resend the same schema about every 10 seconds. That alone must not clear the acknowledged state or pause FRAMEs. Neither these retransmissions nor duplicate ACKs reset schema/frame numbers, state, or usage time.

<a id="frame-recovery"></a>

### 5.4 When new FRAMEs stop arriving

Track two timestamps separately:

| Timestamp | Updated by |
|---|---|
| `last_valid_receive` | A protocol-valid SCHEMA, PONG, or FRAME. |
| `last_frame_at` | A new FRAME fully validated and decoded with the current schema. |

Only the second timestamp tells you that new motion data has arrived. A PONG, schema, ACK retransmission, partial fragment, malformed FRAME, or duplicate/out-of-order FRAME does not update `last_frame_at`. A valid FRAME with `tracking=false` does update it: losing the face is not a network disconnection.

Use the time the initial schema was stored as the **reference time** until the first frame arrives. After that, use the time of the last valid new FRAME.

| Time since the reference time | Receiver action |
|---|---|
| 3 seconds without a new FRAME | Start recovery. |
| Recovery | Make at most 3 attempts, 1 second apart. UDP sends the saved current schema's ACK and GET_SCHEMA with `schema_id=0`. TCP sends only GET_SCHEMA. |
| 12 seconds without a new FRAME | Enter `WAITING`; keep the receive endpoint open. Stop timer-driven HELLO, ACK, GET_SCHEMA, PING, and automatic reconnection. |
| Earlier lease expiry with no valid traffic, or a valid applicable ERROR | May enter `WAITING` before the 12-second frame deadline. Do not exit the program. |

For example, with timely timer execution, the three recovery attempts occur at 3, 4, and 5 seconds. The first ACK sent when adopting a schema is not a recovery attempt. ACKs responding to a schema retransmission are also separate, passive responses with the once-per-second re-ACK limit. A timer-generated ACK and a passive ACK due in the same second may be combined.

The 12-second default allows more time than iOS's independent 10-second ACK wait; do not immediately declare a disconnection when a schema update is awaiting its ACK. During one period without valid frames, a new schema ID or PONG does not reset the reference time or recovery budget. If the schema changes, subsequent re-ACKs refer only to the new current schema; remove queued ACKs for old schemas.

After the receiver has entered `WAITING`, receiving the same or a new schema does not restart exhausted automatic recovery. Validate/store it, finish `on_schema`, and reply with an ACK on UDP. A **valid new FRAME** restores `STREAMING`, normal monitoring, and the three-attempt budget for the next interruption. This requires a schema valid for the current UDP session or the same still-open TCP connection. A new TCP connection must first follow [5.6](#tcp-reconnect).

A freshly started `--listen` receiver has not exhausted recovery. Its first manual SCHEMA therefore starts the initial-frame monitoring described above.

<a id="waiting"></a>

### 5.5 Keep listening after retries stop

**UDP:** Keep using `recvfrom` on the same bound socket. Do not close or recreate the socket, change its port to an automatically assigned one, or send STOP merely to enter `WAITING`. Do not use UDP `connect` to filter to one fixed peer; validate the sender IP and session in the application. A delayed FRAME from the same session may resume reception when its schema and sequence are valid.

**TCP:** Keep reading a connection that is still alive. If it disconnects or the stream is malformed, close only that connection and keep the receiver's TCP listener open. A later manual connection from iOS can start with SCHEMA. Accepting a connection alone does not mean that FRAME reception has started.

While in `WAITING`, timers send no requests or PINGs and make no automatic reconnection attempts. Valid incoming schemas still receive the appropriate UDP ACK. The lease can eventually end the old iOS session while the receiver remains available for a later manual start.

<a id="tcp-reconnect"></a>

### 5.6 Treat a new TCP connection as new

**A new TCP connection needs its own validated initial SCHEMA, even if its remote IP address and port are unchanged.** Do not infer that it is ready from a saved peer/session pair.

Start a 5-second initial-SCHEMA deadline when the connection is established. For an incoming manual connection, this is when the receiver accepts it. Track completion separately for each connection instance. Old session state or partial incoming data must not disable or restart the deadline.

Until a complete initial SCHEMA has been received and validated on that connection and `on_schema` has completed, do not decode or forward its FRAMEs or enter `STREAMING`. An old matching `session_id`/`schema_id`, even with a newer FRAME sequence, is not sufficient. If no complete initial schema arrives within 5 seconds, close that connection and return to listening. Do not accept an unlimited number of concurrent connections.

On disconnection, discard incomplete bytes and stop using that connection's validated state to authorize FRAMEs on a future connection. The last decoded pose and retired-session rejection history may be kept, but they do not replace initial-schema validation. Manual startup still uses a new session ID on each start.

These rules do not prohibit recovery over a TCP connection that never disconnected, nor do they change UDP waiting. After initial-schema validation, the bounded recovery rules in 5.4 still apply.

The reviewed receiver has a defect in this area. See [Appendix C.4](#known-tcp-reconnect); rewriting this document does not fix the code.

<a id="keepalive-stop"></a>

### 5.7 Keepalive and explicit stopping

After adopting a session, the receiver sends PING every **3 seconds** while waiting for numeric data, recovering, or streaming. iOS replies with the same sequence in PONG. In `WAITING`, the receiver stops periodic PINGs.

The iOS **lease** checks whether valid control messages still arrive from the receiver. If none arrives for `lease_ms`, iOS expires the session. PING/PONG does not extend the separate FRAME deadline or the iOS 10-second ACK wait. Every control except HELLO uses the adopted iOS session ID, and iOS checks both that ID and the sender address/TCP connection.

Use STOP for an explicit receiver termination, not because retry attempts ran out. The receiver may also exit on Ctrl+C, `stop_event`, an explicitly set `duration` expiring, a fatal local error such as bind failure, or a callback failure. Preserve the original cause of a callback error. iOS's own Stop action, lifecycle changes, lost permissions, and purchase/trial restrictions still apply.

The waiting behavior does not guarantee recovery from computer sleep, process termination by the OS, a disappearing network interface, or a blocked network/firewall.

<a id="validation"></a>

## 6. Validation and resource limits

A receiver must check more than the message's type. Validate its declared lengths, sender/session, schema, and numeric data before passing a frame to the application. The following limits and rejection rules are part of this protocol, not optional debugging checks.

<a id="limits"></a>

### 6.1 Sizes, names, and incomplete messages

All payload sizes below **exclude the 40-byte common header**.

| Payload | Limit |
|---|---|
| HELLO | Exactly 8 bytes |
| SCHEMA | At most 262144 bytes, including the 20-byte start information |
| Schema JSON | At most 262124 bytes |
| FRAME | Exactly `N × integer_size + 48`; at most 16432 bytes (`4096 × 4 + 48`) |
| ERROR | 1–512 bytes of valid UTF-8 |
| GET_SCHEMA, PING, PONG, STOP, SCHEMA_ACK | 0 bytes |

A schema has **0–4096** BlendShape names. Each is a unique, case-sensitive UTF-8 string of **1–255 bytes**, not an empty string. Reject characters U+0000–U+001F and invalid Unicode. These limits do not fix the list to the 52 standard names.

Require exactly the nine JSON keys defined in 3.4. Reject duplicate keys, missing or unknown keys, and nonstandard constants such as NaN. Require the specified app/profile, units, layout, and encoding. Head/eye values must be finite; reject NaN or infinity. Numeric payload length must match the schema.

| Incomplete data held by a receiver | Limit or expiration |
|---|---|
| Messages being reassembled | At most 8 |
| Actual stored fragment bytes, combined | At most 524288 bytes |
| Fragments in one message | At most 512 |
| Incomplete FRAME | Discard after 250 ms |
| Incomplete SCHEMA | Discard after 3 seconds |
| Incomplete schema for a candidate new session | Only 1 candidate at a time; discard after 3 seconds |

Apply the fixed fragment capacities in 3.8 when checking indexes, counts, lengths, and shared metadata. Matching duplicates may be ignored; conflicting fragments or metadata invalidate the assembly.

<a id="session-validation"></a>

### 6.2 Accepting a schema and a session

Adopt the session ID and its schema together, **after** validating all fragments, start information, and JSON and successfully completing `on_schema`. A partial schema or an early FRAME cannot establish the session. Invalid input must not replace the current schema.

For normal startup, the nonzero `client_nonce` in SCHEMA must match the receiver's HELLO for that startup. For manual startup, accept 0 only when manual receiving is permitted. Keep nonce validation for normal startup; do not remove it to support the manual case. Check the requested/permitted FPS and UDP size limits as well as lease and revision. A session's nonce, FPS cap, UDP size cap, lease, and revision remain unchanged throughout that session.

A **new session** may be adopted only in `WAIT_SCHEMA` or `WAITING`. Reject another session trying to take over during `WAIT_FRAME`, `STREAMING`, or `RECOVERING`. In the reference receiver, `--host` restricts accepted input to the specified host's IP addresses; `--listen` without `--host` explicitly permits any sender on a trusted LAN. Reply to the actual source port on UDP. Once established, check the sender/session and, for TCP, the particular connection instance.

After adopting a new session, discard old schema, fragment, and sequence state. Keep the **32 most recently replaced sender-address/session pairs** so old packets cannot restore a retired session. Only one incomplete candidate-session schema may be held at a time, for at most 3 seconds.

None of these identifiers, the port choice, or manual nonce=0 authenticates a sender. Use a trusted LAN/VPN and do not expose these ports to the public Internet.

<a id="sequence"></a>

### 6.3 Late frames and unknown schemas

FRAME's `sequence` is a UInt32 counter for the session. To compare a received value with the last accepted value, calculate:

```text
difference = (new - old) mod 2^32
newer      = 1 <= difference <= 2^31 - 1
```

Reject duplicates, older/out-of-order frames, and a difference of exactly half the range. The comparison handles UInt32 wraparound. Gaps do not prove network loss: the sender may deliberately drop frames before transmission. A schema change does not reset the frame sequence.

If a FRAME carries an unknown `schema_id`, discard it and request the schema using GET_SCHEMA at most once per second. Before an initial session ID is established, retry HELLO instead. If iOS no longer holds the requested schema, it returns the latest one. Do not let an old schema or ACK roll the current schema back, and do not request old FRAME retransmissions.

<a id="invalid-data"></a>

### 6.4 Reject malformed data

Reject unsupported message types (including 2), unknown flag bits, a `header_size` other than 40, oversized declared lengths, and a payload whose type or length does not match its definition. Apply the schema, Unicode, finite-number, and session checks above before using the data.

For malformed **UDP** data, discard the datagram or affected assembly. For a malformed **TCP** stream, close the affected connection and keep the receiver listener as specified in 5.5. Do not search for `FMV3` inside a corrupted stream and guess where a new message begins.

A valid but stale message is subject to the session/schema/sequence rules; it must not change current state. In particular, a delayed rejection of an earlier HELLO is not an error for the established session. Validate ERROR payloads as UTF-8, including errors rejecting a pending HELLO.

An application callback failure is not a retryable network error. Terminate with its original cause preserved; if `on_schema` fails, send no ACK.

<a id="source-token"></a>

### 6.5 ScrapingValue and source_token

ScrapingValue is carried only as `source_token` in the **FRAME header**, not as a BlendShape name or a schema JSON field. Other message headers, including SCHEMA, have `source_token=0`. A token change takes effect on the next FRAME without changing the schema.

The sender converts the fetched text as follows:

1. Trim only ASCII space, tab, carriage return, and line feed (SP/TAB/CR/LF) from both ends.
2. For an empty result or exactly lowercase `none`, use token 0.
3. Otherwise, apply FNV-1a-32 to the UTF-8 bytes. Start with **2166136261** and, for each byte, calculate `h=((h XOR byte)*16777619) mod 2^32`.
4. If the final hash is 0, replace that hash with 1.

Compute and cache the result when the source value is initialized or updated. Do not fetch the web page or hash the text for every frame, and do not use Swift `hashValue`. Preserve the existing HTML/data-fetching method and successful-value caching behavior.

A token of 0 does not prevent reception. This token is not authentication, encryption, or spoofing protection. Use the trusted-network restrictions in 6.2.

<a id="ios-notes"></a>

## Appendix A. Notes for maintaining the iOS sender

This appendix applies to the iOS implementation. A developer building only a receiver does not need to modify these iOS internals. The compatibility and calculation requirements below remain part of implementing v3 in the existing apps.

<a id="ios-compatibility"></a>

### A.1 Keep existing network paths and settings

Use the existing app receive endpoints, not additional dedicated v3 ports. Dispatch data beginning with `FMV3` to the v3 codec. Pass non-v3 data to the legacy handler, but do not reinterpret malformed v3 data as a legacy text command.

| Existing path | Preserve it as follows |
|---|---|
| iFacialMocap's legacy TCP startup | The legacy string sent to UDP49983 still causes iOS to connect to the receiver's TCP49986. It remains separate from PC-initiated v3 TCP. |
| iFacialMocap direct TCP | Share the existing TCP49984 listener used by `startListener()`. Do not change the separate 49985 listener or the 49987 recorded-data path. |
| Facemotion3d UDP | Keep iOS's standard command listener on 49993. Receiver port 49983 follows the Other output's default and the official Python example. |
| Facemotion3d's existing automatic-connection branch | Some existing code sends to receiver UDP49993. Do not change that legacy branch to 49983. A v3 receiver can explicitly use `--listen-port 49993` when appropriate. |
| Facemotion3d compatibility listener | Keep the existing iOS UDP49983 compatibility listener; do not close or move it. |

Use the user's selected destination for manual sending. Do not overwrite existing defaults or saved `sendPort` / `sendProtocol` settings with temporary v3 session information.

Bulk recording transfer, FBX, audio-file transfer, body data, and DCC-specific bridges stay on their existing paths. Adding v3 does not add, remove, or replace Bluetooth functionality, nor allow taking over another client's active transfer.

<a id="ios-calculations"></a>

### A.2 Build stable schemas and preserve calculations

Build and cache the schema order when settings change. Do not derive it by enumerating a dictionary on every frame. Include the standard fields, `FM_*` fields, and configured custom audio BlendShapes, rather than only the fields currently activated by speech.

Inactive custom fields stay in the schema with a value of 0. If audio intentionally overrides a standard field, use that single field, not a duplicate name; when the override is inactive, preserve its underlying value. Diagnose unintended duplicate names from remapping and do not transmit an ambiguous schema.

Reuse the existing `Int(weight * 100)` / `blendShapePercent` rounding results. Do not independently change calculation order or precision. Send head/eye values after the existing axes, scaling, calibration, and mirroring operations, preserving the profile's meaning. Do not convert them to a new common coordinate system or transmit extra internal head values.

<a id="ios-conditions"></a>

### A.3 Apply the existing usage conditions

Before accepting normal or manual startup, iOS checks purchase/license status, usage-time limits, foreground requirements, permissions, and conflicts with other transfers. v3 must not bypass these restrictions.

Keep face processing and display updates active during schema ACK waits, retaining only the latest outgoing values. If an ACK wait expires, end the unconfirmed send attempt; for manual startup, tell the user that receipt could not be confirmed. A later manual start uses a new session ID.

Retries, re-ACKs, GET_SCHEMA, and repeated HELLO must not reset purchase/trial start times, frame numbers, or usage limits. Stop on the earliest applicable ACK, lease, or usage deadline. Preserve existing stopping behavior for iOS lifecycle events, lost permissions, and the app's Stop button.

<a id="python-notes"></a>

## Appendix B. Using the reference files

`face_motion_v3.py` is the reference receiver. `simulate_ios_v3.py` produces synthetic data for tests without an iPhone; its sender-side ACK/retry logic is in `SchemaDelivery`. The simulator is not a complete production iOS implementation. Read [Appendix C](#known-issues) before using either file as a model for another implementation.

The wire names and Python API names are not always identical:

| In this specification | In the reference Python |
|---|---|
| `message_type` | `Packet.kind` |
| `total_payload_length` | `Packet.total_length` |
| One packet's `chunk_length` | Byte length of that packet's `Packet.payload` |

`V3Client` permits manual receive waiting. The lower-level `ReceiverCore` accepts the manual nonce 0 only when `allow_push=True` is explicitly selected.

| Receiver option | Meaning |
|---|---|
| `--app ifacialmocap` / `--app facemotion3d` | Select app-specific defaults; iFacialMocap is the default. Tr uses iFacialMocap's defaults. |
| `--transport udp` / `--transport tcp` | Select the transport. |
| `--port` | Override the destination port on iOS or the simulator. |
| `--listen-port` | Override the receiver's local listening port. |
| `--host` / `--listen` | Initiate normal startup toward the host, or wait for manual startup. Host filtering still applies when a host is specified. |
| `--bind` | Select the local bind address and one address family. IPv4 is the default; `--bind ::` selects IPv6. |
| `--no-reconnect` | Accepted for compatibility. With or without this option, automatic attempts are bounded and passive listening remains available. |

See [README.md](README.md) for commands and application integration. Real-device testing on Windows, macOS, and IPv6 remains separately necessary.

[Transmit diagram](diagrams/FMV3_PC_to_iOS_EN.png) · [Receive diagram](diagrams/FMV3_iOS_to_PC_EN.png) · [Diagram text](diagrams/DIAGRAM_TEXT_EN.md) · [Expected byte vectors](golden_vectors.json)

The diagram row `HELLO=1 / SCHEMA=3 / FRAME=4` gives examples, not every message type and not handshake order. SCHEMA_ACK is 10. Likewise, “HELLO only: PC client_nonce” describes PC-to-iOS messages; across both directions, a pending-HELLO rejection ERROR also uses that nonce. Use Section 3 for the complete definitions.

The diagrams and byte vectors are reference material, not runtime dependencies. The small sample distribution does not include the maintainer's full automated test suite.

<a id="known-issues"></a>

## Appendix C. Known differences in the reviewed Python snapshot

**This appendix records code behavior, not alternative protocol rules.** The findings were reported in the 2026-09-22 review. They apply to the files identified below, not necessarily to later GitHub revisions or production iOS builds. This document-only rewrite does not modify the code.

| Reviewed file | SHA-256 |
|---|---|
| `face_motion_v3.py` | `d68b145ddf7307f79146cf2d1b93f691246c215a329e89593d73c766dfd0f432` |
| `simulate_ios_v3.py` | `b4d73d14b532470820eace853c5347d059c3a6698bd3e23e40c959dff45ce9f4` |

<a id="known-error-utf8"></a>

### C.1 Pending-HELLO ERROR accepts malformed UTF-8

In `V3Client._handle()`, an otherwise matching ERROR for a pending HELLO uses `decode('utf-8', 'replace')`. Malformed bytes are displayed as replacement characters and move the receiver to `WAITING`. The established-session path in `ReceiverCore.accept()` rejects malformed UTF-8 instead. An ERROR payload containing byte `FF` reproduced the difference.

**Required behavior:** Validate ERROR as UTF-8 in both cases, following 3.7 and 6.4. The permissive branch is not a rule to copy. The separate, already-fixed handling of delayed errors for an earlier HELLO had been regression-tested in the prior review.

<a id="known-token-cache"></a>

### C.2 The simulator hashes source_token on every frame

The simulator's FRAME loop calls `source_token("hapihapi")` for each generated frame. The transmitted value is correct, but the code does not demonstrate the required caching optimization.

**Required behavior:** As in 6.5, a production sender computes the token when the source value is initialized or updated, then reuses it. Do not copy per-frame hashing into the production send loop.

<a id="known-hello-timeout"></a>

### C.3 The simulator does not enforce the HELLO deadline

The simulator's TCP accept loop leaves a connection open even when HELLO has not arrived after 5 seconds, and accepts a later HELLO.

**Required behavior:** In normal TCP startup, iOS closes a connection if no complete HELLO arrives within 5 seconds of accepting it. This is the sender-side deadline in 5.2, not the receiver-side initial-SCHEMA deadline in the next issue.

<a id="known-tcp-reconnect"></a>

### C.4 A new TCP connection can reuse old session state

After a manual TCP connection received SCHEMA and FRAME and then disconnected, a new connection using the same source IP/port could be treated as the old session. The earlier review observed two results:

- With no initial SCHEMA on the new connection, it stayed open for about **5.55 seconds** and accepted a later schema, instead of enforcing the 5-second deadline.
- An old-session FRAME with the previous session/schema IDs and a newer sequence was accepted **before any SCHEMA on the new connection**, returning the receiver to `STREAMING`.

The socket and TCP framing buffer were replaced, but `core.session_id` and `_peer` remained. The deadline and current-session checks did not adequately distinguish the new connection. The review forced source-port reuse on Linux; it did not measure how often this occurs in normal use or establish its effect on production iOS builds.

**Required behavior:** Implement connection-specific initial-SCHEMA validation and the deadline in [5.6](#tcp-reconnect). Retained state must not authorize old-session FRAMEs on a new, unvalidated connection. This is a receiver defect, not permission to skip SCHEMA. It does not prohibit resumption over the same TCP connection that remained open.

A successful exchange with the simulator is not proof that all production requirements are satisfied. App lifecycle, purchase restrictions, and real-device networking still require separate validation.

<a id="history-and-tests"></a>

## Appendix D. Design history and implementation checks

The history explains existing assignments. It does not add another startup mode or authorize an older wire format. The checks are for implementers and maintainers, not a report that every check has been run on a particular product.

<a id="history"></a>

### D.1 Why message type 2 is unused

An earlier v3 draft assigned type 2 to **WELCOME**, an iOS-to-receiver message accepting a startup request. That separate response was removed, and its start information was merged into SCHEMA. The other message IDs were retained. SCHEMA_ACK kept ID 10, and 2 was left unused rather than assigned a different meaning.

WELCOME and SCHEMA_ACK have different purposes and directions: the former accepted a request on the iOS side; the latter confirms that the receiver has validated and stored a schema. In `contract_revision = 4`, do not implement WELCOME or send/accept type 2.

“Reserved” here means **unused and prohibited**, not freely available for a custom message or promised for a future feature. The gap adds no byte, padding, empty packet, wait step, or buffer slot. It does not ban the number 2 in other fields that permit it. This history belongs to v3 design, not to v1/v2 text protocols or app version numbers; leaving the ID unused does not add compatibility with the older draft.

<a id="acceptance-tests"></a>

### D.2 Required implementation checks

| Area | Check |
|---|---|
| Type IDs and wire layout | ACK is 40 bytes with no payload and byte offset 4 equals `0x0A`. Reject `0x02`, unsupported types, and revisions other than 4. Do not add reserved padding or handshake messages. |
| Initial UDP startup | HELLO leads to SCHEMA. No FRAME is sent before a valid current-schema ACK. A receiver does not ACK a partial or invalid schema. |
| Missing SCHEMA or ACK | Lose all or part of SCHEMA, then separately lose only the first ACK. Both cases recover through schema retransmission and acknowledgement. |
| Schema changes | Test additions, removals, renames/reordering, i16→i32, another change while awaiting ACK, duplicate SCHEMA/ACK, and delayed old FRAMEs. Wrong-schema, wrong-session, or wrong-sender ACKs must not resume sending. Resume with the latest values only. |
| Sender deadlines | PING, repeated HELLO, and repeated settings changes do not extend the 10-second ACK deadline. Normal TCP's incomplete HELLO connection closes after 5 seconds. |
| Receiver monitoring | Test no FRAME after ACK, interrupted FRAMEs, schema-update waits, and SCHEMA-only traffic. Even if only PONGs continue for more than 35 seconds, the receiver has entered WAITING after bounded recovery; PONGs must not keep FRAME monitoring alive. |
| Waiting and manual restart | Retain the UDP port and TCP listener. Stop timer-driven requests/PING and do not send STOP merely for waiting. Re-ACK valid saved/new manual schemas; resume on valid FRAME. Test `--listen` with no HELLO on both transports. |
| TCP startup and reconnection | Normal startup is HELLO→SCHEMA→FRAME without SCHEMA_ACK. After disconnect, test a new connection using the same source IP/port: enforce its own 5-second initial-SCHEMA deadline; never forward an old-session FRAME before its SCHEMA. Verify that a valid new manual SCHEMA/session then FRAME works. |
| Session and error checks | Reject wrong IP/nonce, retired sessions, and takeover of a running stream. Handle matching pending-HELLO ERRORs but ignore delayed errors for an earlier HELLO after a session is adopted. Validate UTF-8. |
| Application callbacks | If `on_schema` fails, send no ACK and terminate with the original error. |
| Values and app behavior | Test negative values, values above 100, Unicode, type boundaries, head/eye scaling, mirroring, playback, tracking loss, stop/reconnect, purchase limits, and regressions in legacy v1/v2 paths. |

Use `golden_vectors.json` for byte comparisons and the README's two-terminal procedure for a basic simulated exchange. The maintainer's full test suite is not included in the small distribution. Completing iOS implementation also requires an Xcode build and real-device UDP/TCP interoperability testing for both app families. Do not report unperformed checks as successful.

<a id="references"></a>

### D.3 General references

These references explain general mechanisms. The FMV3-specific fields, numbers, and behavior are defined in this document.

[Python struct](https://docs.python.org/3/library/struct.html) · [Python Socket HOWTO](https://docs.python.org/3/howto/sockets.html) · [UDP Usage Guidelines — RFC 8085](https://www.rfc-editor.org/rfc/rfc8085.html) · [TCP — RFC 9293](https://www.rfc-editor.org/rfc/rfc9293.html)
