# Face Motion v3 — Receiver specification

[日本語](PROTOCOL_V3_JA.md) · [README](README.md) · [Changes](CHANGELOG.md)

**Contract revision 5.** This document is for people who write a program that *receives* motion data from **iFacialMocap**, **iFacialMocapTr** or **Facemotion3d**. Below, the program that receives the data on a PC or an embedded device is called the "**receiver (PC)**", and the iPhone/iPad app that sends it is called "**iOS**".

In Face Motion v3 (FMV3), iOS first sends a **SCHEMA** (the table) once: the **list of BlendShape names** and **how to read the numbers** (for example, how many bytes one value uses). After that, every frame is a **FRAME** that carries **only numbers, in the same order as the BlendShape name list in the SCHEMA**. The head and eye values have no names; they always follow the BlendShape numbers in a fixed order.

When a SCHEMA arrives, the receiver (PC) builds a "name → position" map once. After that it never parses text per frame; it just reads numbers by position.

| | |
|---|---|
| Transport | **UDP** (recommended) or TCP |
| Byte order | Little-endian, no padding. A number of two or more bytes is written **lowest byte first** (for example, 1200 is `04B0` in hexadecimal, so it is sent as `b0 04`). There are **no empty bytes** between fields. A C struct copied as a whole may contain gaps added by the compiler, so read and write one field at a time |
| Message shape | Every message is "a 40-byte **header** + a **body** (payload)". The header is the common envelope of every message and holds its type and length. The **body (payload)** is the content that follows the header and is specific to the message (for a FRAME, the numbers). Some messages have no body |
| BlendShape list | Not fixed at 52. The number depends on the app and its settings (custom BlendShapes, voice-input settings and so on). It normally stays the same during one stream, but **it can change in the middle of a stream**, for example when playback starts of a recording with another BlendShape list. When it changes, a new SCHEMA arrives ([section 4](#changes)) |
| Security | None. Use a trusted LAN only |

Contents: [1 Basics](#basics) · [2 UDP step by step](#udp) · [3 Messages](#messages) · [4 Schema changes](#changes) · [5 Timers and recovery](#timers) · [6 TCP](#tcp) · [7 Limits and validation](#limits) · [8 Compatibility](#compat)

<a id="basics"></a>
## 1. Basics

### 1.1 Ports

Each side listens on exactly one port per transport. There are no fallback ports.

| App | iOS listens (UDP / TCP) | Receiver (PC) listens (UDP / TCP) |
|---|---|---|
| iFacialMocap, iFacialMocapTr, Facemotion3d ("Other" or "Unity" license) | 49983 / 49984 | 49983 / 49984 |

Facemotion3d needs its "Other" or "Unity" license. With either license, v3 sends the "Other" output (the iFacialMocap-compatible output). v3 therefore uses the same ports as iFacialMocap, and the receiver (PC) does not need to know which app is sending.

- **Normal start**: the receiver (PC) knows the phone's address and contacts the **iOS port**.
- **Manual start**: the user types the receiver's address into the iOS app, and iOS contacts the **receiver (PC) port**.

If a receiver (PC) port is already used by another program, report that clearly and stop. Do not pick another port on your own. Users can change ports on both sides.

### 1.2 Terms

| Term | Meaning |
|---|---|
| Session | One stream, from the moment iOS starts sending until it stops. Each time iOS starts sending, it picks a number to tell this stream apart from others, `session_id` (a random nonzero 64-bit value), and puts it in the header of every message of that session. The receiver (PC) uses it to tell whether data belongs to the stream it is receiving, so that late data from an earlier stream is never mixed in |
| Table (SCHEMA) | The BlendShape name list and how to read the numbers. Within a session, tables are numbered by `schema_id` (1, 2, …); the number grows each time the order of names or the value type (i16→i32) changes |
| `client_nonce` | A random nonzero 64-bit value the receiver (PC) picks for each normal start. "Nonce" means "a number used only once", and this value is also called "the HELLO nonce". The receiver (PC) puts it in the `session_id` field of the HELLO header, and iOS copies it unchanged into the SCHEMA start information ([3.4](#schema)). By checking that the SCHEMA's `client_nonce` equals the value in its own HELLO, the receiver (PC) confirms that this SCHEMA answers its current HELLO, and is not an old SCHEMA from an earlier run or one meant for another PC. A value of 0 in SCHEMA means iOS started the stream (manual start) |
| Revision | `contract_revision`, the version of this wire contract: **5** |

### 1.3 What a FRAME contains

- **BlendShape values**: one signed integer per name of the SCHEMA, in the same order. The unit is *percent*: `25` means 0.25, `-25` means −0.25 and `150` means 1.5. Values below 0 and above 100 are valid.
- **Head**: rotation x, y, z in degrees and position x, y, z in meters (after the app's own scaling, axis settings and calibration).
- **Right eye** and **left eye**: rotation x, y, z in degrees.
- **State**: whether the face is tracked (tracking; during playback, whether it was tracked when the frame was recorded) and whether a recording is being played back (playback), in the header's `flags` ([3.2](#header)).

<a id="udp"></a>
## 2. UDP step by step

```text
Receiver (PC)                                   iOS
  bind UDP 49983
  |---- HELLO (1) ------------------------------->|  to iOS UDP 49983
  |<--- SCHEMA (2), usually about 3 datagrams ----|
  |     reassemble, validate, store               |
  |---- SCHEMA_ACK (3) --------------------------->|
  |<--- FRAME (4) --------------------------------|  repeated, up to the agreed FPS
  |---- PING (6) every 3 s ----------------------->|
  |<--- PONG (7) ---------------------------------|
  |---- STOP (8) when the receiver (PC) quits ---->|
```

The numbers in brackets are message type numbers ([3.1](#types)).

1. **Bind** a UDP socket to the receiver (PC) port. Use this one socket for everything: iOS replies to the address and port the receiver (PC) sends from.
2. Pick a new random `client_nonce` (the number of this HELLO, [1.2](#basics)) and **send HELLO**. If no SCHEMA arrives, send the same HELLO again every second, 5 times in total.
3. **Receive SCHEMA.** It may arrive in several datagrams (fragments, [3.9](#fragments)). Join them, check that `client_nonce` equals your nonce, validate the JSON, and store the table.
4. **Send SCHEMA_ACK** for that `schema_id`. iOS sends no FRAME before this ACK.
5. **Receive FRAMEs.** Use the stored table to read the numbers. Skip frames that are older than the last one you used ([3.6](#frame)).
6. While the session runs, **the receiver (PC) sends PING to iOS every 3 seconds**. When iOS hears nothing from the receiver (PC) for `lease_ms` (normally 10 s), it decides the receiver (PC) is gone and stops sending.
7. **Send STOP** when the receiver (PC) quits on purpose.

**Why PING is needed.** UDP has no "connection". If the receiver program quits or the PC leaves the network, iOS cannot notice. Without PING, iOS would keep sending to nobody and keep using battery and network. A sending iOS also refuses other receivers (`BUSY`), so nobody could start again from another PC. The rule "the receiver (PC) sends PING regularly, and iOS stops by itself when PINGs stop" prevents this. The PONG that iOS returns also tells the receiver (PC) that iOS is still running, including while FRAMEs pause (for example, during a schema change). TCP uses the same rule.

When iOS refuses a HELLO, it answers with ERROR instead of SCHEMA. This happens mainly when iOS is already streaming to another receiver, when the license or trial time does not allow streaming now, when the app is not on its tracking screen, when "Settings → Other functions → No connection accepted from PC" is on in Facemotion3d, or when the HELLO has another revision (full list in [3.8](#error)). Show the text to the user. In iFacialMocap and iFacialMocapTr, v1/v2 streaming that a PC handshake started is an exception: a v3 HELLO replaces it instead of getting `BUSY` (details in [3.8](#error)).

**When iOS stops sending.** When sending stops on the iOS side, no message tells the receiver (PC) (STOP goes from the receiver (PC) to iOS; only when the stream ends with an error does an ERROR arrive). When the user presses Stop in the app, leaves the tracking screen, or the trial time ends, over UDP the FRAMEs simply stop. The receiver (PC) tries to repair as in [section 5](#timers) and then waits (WAITING). Over TCP the connection closes, so the receiver (PC) waits right away. In Facemotion3d with neither the "Other" nor the "Unity" license, iOS does not refuse the HELLO: it starts sending and stops by itself after about 10 seconds.

**Manual start (UDP).** The receiver (PC) only binds its port and waits. iOS sends SCHEMA with `client_nonce = 0`; the receiver (PC) stores it and sends SCHEMA_ACK to the sender's address, then FRAMEs follow. Each press of Start in the app creates a new session.

<a id="messages"></a>
## 3. Messages

<a id="types"></a>
### 3.1 Message types

The **message type number** is one byte that says what a message is. It is the fifth byte of the header (`message_type` at offset 4). Look at it first to decide how to read the rest. The numbers follow the order of a normal UDP start (HELLO → SCHEMA → SCHEMA_ACK → FRAME).

| Type | Name | Direction | Body | Purpose |
|---:|---|---|---|---|
| 1 | HELLO | receiver (PC) → iOS | 8 bytes | Asks iOS to start sending |
| 2 | SCHEMA | iOS → receiver (PC) | 20 bytes + JSON | BlendShape name list, how to read the numbers, session settings |
| 3 | SCHEMA_ACK | receiver (PC) → iOS | none | "The whole SCHEMA arrived and is stored" (UDP only) |
| 4 | FRAME | iOS → receiver (PC) | numbers | The values of one frame |
| 5 | GET_SCHEMA | receiver (PC) → iOS | none | Asks iOS to send the SCHEMA again |
| 6 | PING | receiver (PC) → iOS | none | "The receiver (PC) is still receiving" |
| 7 | PONG | iOS → receiver (PC) | none | Answer to PING |
| 8 | STOP | receiver (PC) → iOS | none | Asks iOS to end the session |
| 9 | ERROR | iOS → receiver (PC) | UTF-8 text | Reason for a refusal or a session error |

Any other type number is invalid (revision 4 used 3 for SCHEMA and 10 for SCHEMA_ACK; see [8](#compat)).

<a id="header"></a>
### 3.2 Header (40 bytes)

The 40 bytes at the start of every message. Python can read and write it with `struct.Struct("<4sBBHQIIIIHHI")`.

**How to read format strings.** A string such as `<4sBBHQIIIIHHI` is a format of Python's `struct` module that describes a sequence of bytes. From left to right, each letter (`4s`: two characters) stands for one value. In other languages, read and write one field at a time using the offsets and types in the tables.

| Format letter | Meaning | Bytes |
|---|---|---:|
| `<` | Little-endian, no padding (no gaps between values) | — |
| `4s` | 4-byte string | 4 |
| `B` | UInt8 (unsigned integer) | 1 |
| `H` | UInt16 (unsigned integer) | 2 |
| `I` | UInt32 (unsigned integer) | 4 |
| `Q` | UInt64 (unsigned integer) | 8 |
| `h` / `i` | Int16 / Int32 (signed integer; FRAME BlendShape values) | 2 / 4 |
| `f` | Float32 (decimal; FRAME head and eye values) | 4 |

For example, the HELLO body `<HHI` means "UInt16, UInt16, UInt32 in this order, 8 bytes in total", and the SCHEMA start information `<QHHII` means "UInt64, UInt16, UInt16, UInt32, UInt32 in this order, 20 bytes in total".

The header fields:

| Offset | Size | Field | Content |
|---:|---:|---|---|
| 0 | 4 | `magic` | ASCII `FMV3`, the mark of an FMV3 message |
| 4 | 1 | `message_type` | The message type number of [3.1](#types) |
| 5 | 1 | `flags` | FRAME state (below). 0 on other messages |
| 6 | 2 | `header_size` | Always 40 |
| 8 | 8 | `session_id` | The session number ([1.2](#basics)). Nonzero |
| 16 | 4 | `schema_id` | The table this message uses (below) |
| 20 | 4 | `sequence` | A counter (below) |
| 24 | 4 | `source_token` | FRAME only ([3.6](#frame)). Otherwise 0 |
| 28 | 4 | `total_payload_length` | Size of the whole body in bytes (before splitting) |
| 32 | 2 | `part_index` | Which piece this is when split, counted from 0 (below) |
| 34 | 2 | `part_count` | How many pieces the body was split into (below) |
| 36 | 4 | `chunk_length` | Body bytes in this datagram / message |

- **`flags`** (FRAME state): bit 0 (value 1) is tracking: 1 means the face is tracked, 0 means the face is lost. With live values it is the current state: whether the camera tracks the face now. During playback it is the state recorded with that frame: whether the face was tracked when the frame was recorded. Only for an old recording that has no tracking state does it show the current camera state. Bit 1 (value 2) is playback: 1 means the values come from a recording being played back, 0 means live camera values. Examples: `1` = live and tracked, `0` = live and face lost, `3` = playback of a frame in which the face was tracked when it was recorded, `2` = playback of a frame in which the face was lost when it was recorded.
- **`session_id`**: which session the message belongs to. iOS picks it when it starts sending and tells the receiver (PC) in the SCHEMA header. After that, iOS and the receiver (PC) put this same number in every message of the session. When a HELLO is sent there is no session yet (no number has been picked), so a HELLO carries the receiver's own `client_nonce` instead.
- **`schema_id`**: the number of a table (SCHEMA). The table can change even within one session (for example when playback starts of a recording with another BlendShape list; [section 4](#changes)), so every message that refers to a table says which one. On a SCHEMA it is the number of that table, on SCHEMA_ACK the table that was received and stored, on a FRAME the table to read the numbers with, and on GET_SCHEMA the table to send again (see the example below).
- **`sequence`**: on a FRAME, a counter that grows by 1 per frame; the receiver (PC) uses it to spot old and duplicate frames. On PING/PONG, it pairs an answer with its PING.
- **`part_index`, `part_count`, `chunk_length`**: over UDP, a body that does not fit into one datagram (one packet of UDP data) is sent in pieces. `part_count` is the number of pieces, `part_index` is which piece this is (from 0), and `chunk_length` is the body bytes in this piece. Without splitting: `part_index = 0`, `part_count = 1`, `chunk_length = total_payload_length` (`part_count` is 1, not 0).
  - **FRAME** is normally not split. For example, 52 BlendShapes as i16 take 192 bytes; with the 1200-byte limit, up to 556 fit without splitting.
  - **SCHEMA** is **normally split** over UDP. A SCHEMA datagram is limited to 576 bytes, and even the standard 52 names make a body of about 1,100 bytes, so it arrives in about 3 pieces. A receiver (PC) must at least be able to join SCHEMA pieces ([3.9](#fragments)).
  - **TCP** never splits (`part_count = 1` always).

Values per type:

| Type | Value of `session_id` | Value of `schema_id` | Value of `sequence` |
|---|---|---|---|
| HELLO | the `client_nonce` picked by the receiver (PC) (there is no session yet) | 0 (unused) | 0 (unused) |
| SCHEMA | this session's `session_id`, picked by iOS | the number of this table (starts at 1 and grows by 1 each time the order of BlendShape names or the value type changes) | 0 (unused) |
| SCHEMA_ACK | the same `session_id` as the received SCHEMA | the `schema_id` of the SCHEMA that was received and stored | 0 (unused) |
| FRAME | this session's `session_id` | which table to read these numbers with (that table's `schema_id`) | the frame counter |
| GET_SCHEMA | this session's `session_id` | the `schema_id` of the table to send again; 0 = the newest table | 0 (unused) |
| PING | this session's `session_id` | 0 (unused) | a number the receiver (PC) picks (1, 2, 3, …) |
| PONG | this session's `session_id` | 0 (unused) | the same number as the received PING |
| STOP | this session's `session_id` | 0 (unused) | 0 (unused) |
| ERROR | this session's `session_id`, or the HELLO's `client_nonce` when refusing a HELLO | 0 (unused) | 0 (unused; ignore other values) |

How `schema_id` is used (UDP):

```text
iOS -> receiver (PC)   SCHEMA      schema_id=1  blend_names = [eyeBlinkLeft, jawOpen, mouthSmileLeft]
receiver (PC) -> iOS   SCHEMA_ACK  schema_id=1   <- table 1 is stored
iOS -> receiver (PC)   FRAME       schema_id=1  numbers = [10, 45, 3]      <- 3 values in table-1 order
    (for example, playback starts of a recording that has one more BlendShape)
iOS -> receiver (PC)   SCHEMA      schema_id=2  blend_names = [eyeBlinkLeft, jawOpen, mouthSmileLeft, myCustomSmile]
receiver (PC) -> iOS   SCHEMA_ACK  schema_id=2   <- table 2 is stored; iOS now sends in table-2 order
iOS -> receiver (PC)   FRAME       schema_id=2  numbers = [10, 45, 3, 80]  <- 4 values in table-2 order
```

A FRAME's `schema_id` tells the receiver (PC) which table to read its numbers with, so frames sent around a table change are never mixed up. The `schema_id` in SCHEMA_ACK lets iOS confirm which table reached the receiver (PC).

### 3.3 HELLO

40-byte header + 8-byte body, 48 bytes in total. The body format is `<HHI` (UInt16, UInt16, UInt32 in this order; how to read formats: [3.2](#header)):

| Body offset | Type | Field | Value |
|---:|---|---|---|
| 0 | UInt16 | `requested_fps` | 1–60, the highest frame rate the receiver (PC) wants |
| 2 | UInt16 | `max_udp_size` | 576–1200, largest FRAME datagram including the 40-byte header |
| 4 | UInt32 | `contract_revision` | 5 |

Example for 60 fps and 1200 bytes: `3c 00 b0 04 05 00 00 00`.

The `session_id` field of the HELLO header carries the `client_nonce` (the HELLO nonce, [1.2](#basics)) that the receiver (PC) picked for this normal start. iOS returns this value in the SCHEMA start information, so the receiver (PC) can confirm that the SCHEMA answers its own HELLO.

Repeat an unanswered HELLO unchanged (same nonce, same body). If the same HELLO reaches a running session, iOS only sends its SCHEMA again; the session is not restarted.

<a id="schema"></a>
### 3.4 SCHEMA

Body = 20 bytes of **start information** + UTF-8 **JSON**. The start information format is `<QHHII` (UInt64, UInt16, UInt16, UInt32, UInt32 in this order; how to read formats: [3.2](#header)):

| Body offset | Type | Field | Meaning |
|---:|---|---|---|
| 0 | UInt64 | `client_nonce` | Normal start: the `client_nonce` (the HELLO nonce) that the receiver (PC) sent in the `session_id` field of the HELLO header, copied back unchanged by iOS. The receiver (PC) checks that it equals its own value. Manual start: 0, because there is no HELLO |
| 8 | UInt16 | `actual_fps` | The highest frame rate iOS will actually use: 1 up to `requested_fps`. Manual start: 1–60 (there is no HELLO, so the receiver (PC) has requested nothing) |
| 10 | UInt16 | `max_udp_size` | Largest FRAME datagram including the header: 576 up to the requested value. Manual start: 576–1200 |
| 12 | UInt32 | `lease_ms` | How long iOS waits for anything (such as PING) from the receiver (PC), in milliseconds. When nothing arrives for this long, iOS decides the receiver (PC) is gone and ends the session. 5000–60000, normally 10000 (10 s). The receiver (PC) sends PING more often (every 3 s) |
| 16 | UInt32 | `contract_revision` | 5 |

These values stay the same for the whole session. Example JSON:

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

| Key | Meaning | What the receiver (PC) does |
|---|---|---|
| `schema_version` | Version of this JSON format | Must be `1`. Another number means the keys below changed meaning |
| `app` | Name of the sending app (for example `"iFacialMocap"`, `"Facemotion3d"`) | For display and logs. Accept any value, including new names |
| `profile` | Name of the app's output type | Same as `app` |
| `blend_names` | The BlendShape name list. FRAME numbers follow this order | 0–4096 unique names, each 1–255 UTF-8 bytes, no control characters U+0000–U+001F (see below) |
| `blend_encoding` | Size of one BlendShape value: `"i16"` = 2-byte signed integer (−32768 to 32767), `"i32"` = 4-byte signed integer | Must be `"i16"` or `"i32"` |
| `blend_unit` | Unit of the BlendShape values: `"percent"`. 0 to 100 is the usual range and `25` means 0.25 (25 %). Values such as −25 or 150 also occur | Must be `"percent"` |
| `pose_layout` | Name of the order of the 12 decimals after the BlendShapes. `head_rxyz_pxyz_rightEye_rxyz_leftEye_rxyz` means head rotation x, y, z, head position x, y, z, right eye rotation x, y, z, left eye rotation x, y, z | Must be this value |
| `rotation_unit` | Unit of rotations: `"degree"` = degrees | Must be `"degree"` |
| `position_unit` | Unit of positions: `"meter"` = meters | Must be `"meter"` |
| any other key | Information a future revision may add | Ignore it |

When a "must be" key has another value, the numbers cannot be read correctly, so reject that SCHEMA. When a table arrives, build the "name → position" map.

**Names.** A name is compared as it is, without Unicode normalization: two names are the same only when their sequences of Unicode code points (equivalently, their UTF-8 bytes) are equal. For example, `é` written as U+00E9 and `é` written as U+0065 U+0301 are two different names, even though they look the same. This rule applies to the uniqueness check, to the "name → position" map and to deciding whether a name changed ([section 4](#changes)). The forbidden control characters are exactly U+0000–U+001F. Other characters, including U+007F and U+0080–U+009F, are allowed.

### 3.5 SCHEMA_ACK

40 bytes, no body: type number 3, the SCHEMA's `session_id`, the stored `schema_id`. Send it only after **every** fragment has arrived and the whole table is valid and stored. Only UDP uses it.

Example: `46 4d 56 33 03 00 28 00 08 07 06 05 04 03 02 01 01 00 00 00 …` (the rest is zero except `part_count = 1`; the full 40 bytes are in `golden_vectors.json`).

<a id="frame"></a>
### 3.6 FRAME

A FRAME contains these parts, from left to right.

| Part | Header | BlendShape values | Head | Right eye | Left eye |
|---|---|---|---|---|---|
| Content | Common header ([3.2](#header)) | N integers in the order of `blend_names` | rotation rx, ry, rz; position px, py, pz | rotation rx, ry, rz | rotation rx, ry, rz |
| Type | — | i16 or i32 (the SCHEMA's `blend_encoding`) | Float32 × 6 | Float32 × 3 | Float32 × 3 |
| Bytes | 40 | i16: 2 × N, i32: 4 × N | 24 | 12 | 12 |

`N` is the number of `blend_names`. The head and both eyes are 12 Float32 values, 48 bytes together (format `<12f`). For 52 names as i16: 40 + 104 + 48 = **192 bytes**.

- `sequence` counts frames in the session. A frame is newer when `1 ≤ (new − last) mod 2³² ≤ 2³¹ − 1` (new is the received number, last is the last one used). Skip duplicates and older frames. Gaps are normal: iOS drops frames it could not send in time and never resends them.
- A FRAME for a `schema_id` you do not have yet: skip it and send GET_SCHEMA (at most once per second).
- A FRAME for an older `schema_id`: skip it. Never read it with the newer table.
- Face lost: bit 0 of `flags` is 0 (during playback: the face was lost when the frame was recorded; [3.2](#header)). The numbers are still a valid frame.
- `source_token`: a UInt32 the app sets for its own use. It is not part of the table and not authentication. `0` means "not available". The receiver (PC) may ignore it.

### 3.7 GET_SCHEMA, PING, PONG, STOP

Header only (body 0 bytes).

- **GET_SCHEMA**: "please send table `schema_id` (0 = the newest) again". iOS sends the newest table.
- **PING / PONG**: keepalive ([section 2](#udp)). iOS copies `sequence` into PONG.
- **STOP**: ends the session. Send it when the receiver (PC) quits on purpose, not because retries ran out.

<a id="error"></a>
### 3.8 ERROR

UTF-8 text, 1–512 bytes. Discard an ERROR whose text is not valid UTF-8. When iOS refuses a HELLO, the header carries the HELLO's `client_nonce`. Apply such a refusal only while that HELLO is still unanswered, and never to a session you have already accepted. Otherwise the header carries the session.

Main reasons (other texts can also arrive; display the received text as it is):

| Text | Meaning |
|---|---|
| `BUSY` | iOS is already streaming v3 to another receiver, is sending with v1/v2 or Bluetooth, or is transferring a recording. In iFacialMocap and iFacialMocapTr, this is also the reason while an FBX file is being exported and while a destination is set on the iOS side (a manual start or a v1/v2 destination). Exception: in these two apps, v1/v2 streaming that a PC handshake started is replaced instead (below) |
| `NOT_ALLOWED` | Purchase state or trial time does not allow streaming now |
| `PC_CONNECTION_DISABLED` | "Settings → Other functions → No connection accepted from PC" is on in Facemotion3d (the answer to a UDP HELLO). While this setting is on, iOS does not listen on TCP 49984, so over TCP the connection itself fails. A manual start from iOS still works |
| `V3_NOT_AVAILABLE`, `APP_INACTIVE` | The app is not showing its tracking screen, is in the background, or cannot use the camera. In iFacialMocapTr this is also the reason when trial conditions do not allow streaming now, and in Facemotion3d while an FBX file is being exported |
| `CAMERA_UNAVAILABLE` | The device does not support face tracking, or camera access is not allowed (iFacialMocap, iFacialMocapTr) |
| `SCHEMA_NOT_READY` | The app could not prepare the first table within 5 s |
| `UNSUPPORTED_CONTRACT: requires 5` | The HELLO had another revision |
| `INVALID_HELLO`, `HELLO_SETTINGS_CHANGED` | HELLO values out of range, or the same nonce was sent with different settings |
| `SCHEMA_ACK_TIMEOUT` | UDP: no valid SCHEMA_ACK within 10 s, so iOS ended the session |

**v1/v2 streaming started by a PC (iFacialMocap, iFacialMocapTr).** In these apps the newest PC handshake wins over v1/v2 streaming that an earlier PC handshake started. A v3 HELLO from a receiver (PC) is therefore not refused because of such streaming:

- **Replaced by the v3 HELLO**: v1/v2 sending to the address that iOS learned from a PC's UDP handshake (with the iOS destination setting "default", this includes the TCP connection that iOS opened to the PC after such a handshake), and a v1/v2 TCP connection that a PC opened to iOS. iOS stops this sending, closes these connections and forgets the learned address, then starts the v3 session. The v1/v2 receiver gets no message; its data just stops. The v1/v2 sending does not resume when the v3 session ends.
- **Still `BUSY`**: a destination that the user set on the iOS side (a fixed IP address, an address entered with the Live button, or the destination of a v3 start from iOS), a Bluetooth connection (including a recording transfer over Bluetooth), exporting an FBX file, and a v3 session that is already running (with another receiver, or started from iOS).
- **Not affected**: a recording transfer over TCP (a PC pulling recorded frames from port 49984). It does not make the HELLO `BUSY`, and the v3 HELLO does not end it.

The opposite direction is also product behavior: while v3 is not selected as the sending protocol in the iOS app's settings (or the app's own v3 connection to its saved destination has failed), a new v1/v2 handshake from a PC ends a running v3 session that a PC started. The v3 receiver (PC) gets no message; the stream stops as described in "When iOS stops sending" ([section 2](#udp)).

<a id="fragments"></a>
### 3.9 UDP fragments

SCHEMA and FRAME may be larger than one datagram. The body is then cut into equal pieces, and the last piece holds the rest. SCHEMA's 20-byte start information appears only once, at the start of the joined body.

| Message | Largest datagram | Body per piece (`capacity`) |
|---|---|---|
| SCHEMA | 576 bytes, always | 536 |
| FRAME | `max_udp_size` from the start information | `max_udp_size − 40` |

`part_count = max(1, ceil(total_payload_length / capacity))`. All pieces share every header field except `part_index` and `chunk_length`. Pieces can arrive in any order. Identical duplicates are fine; a piece that contradicts others invalidates that message. Other messages are never split.

How much splitting to expect:

| Message | Example | Splitting |
|---|---|---|
| SCHEMA | the standard 52 names (body about 1,100 bytes) | 3 pieces (`part_count = 3`) |
| FRAME | 52 names as i16 (body 152 bytes, 192 bytes in total) | none (`part_count = 1`). With the 1200-byte limit, up to 556 i16 values fit without splitting |

A SCHEMA is sent before the datagram size has been agreed (the `max_udp_size` in its start information), and a manual start has no HELLO at all. It is therefore fixed at 576 bytes, a size every receiver (PC) can take. FRAME uses the `max_udp_size` agreed in the SCHEMA.

<a id="changes"></a>
## 4. Schema changes

A new `schema_id` means the numbers are ordered differently: BlendShapes were added, removed, renamed or reordered, or the encoding changed from i16 to i32. A name counts as renamed whenever its code points (its UTF-8 bytes) change, even when it looks the same ([3.4](#schema)). The `sequence` counter continues.

For every FRAME it sends, iOS compares the order of BlendShape names and the value type with the current table, and sends a new table when they differ. Within one stream, the table changes mainly in these cases:

- Playback of a recording starts, and the recording's order of BlendShape names or its value type differs from the current table. With the same list and type the table stays the same, and there is no SCHEMA and no SCHEMA_ACK (whether values come from playback is shown by the FRAME `flags`, [3.2](#header)).
  - iFacialMocap and iFacialMocapTr: a recording's BlendShape names are sent in the same alphabetical order as live values, so a recording with the live BlendShapes keeps the table. The value type is decided from the whole recording (i32 if any value does not fit i16).
  - Facemotion3d: the order of names changes only when the recording contains a BlendShape name that is not in the current list (names missing from the recording are sent with the value 0). The value type is decided frame by frame, as in the next item.
  - When playback ends and live values return, the table changes again if the order of names differs from the live one.
- A BlendShape value no longer fits the i16 range (−32768 to 32767). The encoding becomes i32 and does not go back to i16 until the stream ends. Usual values are about 0 to 100, so this is rare.

About the BlendShapes used by Facemotion3d's voice-input feature: the BlendShapes registered in the voice-input settings are in the list from the start of the stream, and are sent with the value 0 while they are not reacting. Speaking therefore never changes the number of names in the list. Facemotion3d's BlendShape settings (the base list, the BlendShapes registered for voice input, name replacement) are fixed when the stream starts; changes apply from the next stream.

- **UDP**: iOS stops FRAMEs, sends the new SCHEMA, waits for its SCHEMA_ACK, then continues with the newest values.
- **TCP**: iOS writes the new SCHEMA before the first FRAME that uses it.

The receiver (PC) switches to the new table only after the new SCHEMA is complete and valid. Until then it may keep decoding FRAMEs of the old `schema_id` with the old table. A given `session_id` + `schema_id` always means exactly the same bytes; if a repeated SCHEMA differs, it is invalid.

When iOS receives GET_SCHEMA or a repeated HELLO, it sends the current SCHEMA again (over UDP at most once per second). Over UDP it also resends it every second while no SCHEMA_ACK has arrived. After SCHEMA_ACK has arrived, it does not resend unless asked. When a table you already stored arrives, reply with SCHEMA_ACK again (UDP), at most once per second.

<a id="timers"></a>
## 5. Timers and recovery

All times are defaults in seconds. "Valid" means a message that passed every check in section 7.

| Who | Waits for | Limit | Then |
|---|---|---|---|
| Receiver (PC) | SCHEMA after HELLO (UDP) | 5 HELLOs, 1 s apart | Stop sending; keep waiting (WAITING) |
| Receiver (PC) | TCP connection to iOS | 3 s per attempt, 5 attempts, 1 s apart | Keep waiting (WAITING) |
| Receiver (PC) | complete first SCHEMA on a new TCP connection | 5 s from connect/accept | Close that connection only |
| Receiver (PC) | a new valid FRAME | 3 s | Repair: up to 3 times, 1 s apart |
| Receiver (PC) | a new valid FRAME | 12 s | WAITING |
| Receiver (PC) | — | every 3 s | PING (not while WAITING) |
| iOS | SCHEMA_ACK (UDP) | resend SCHEMA every 1 s, give up after 10 s | ERROR `SCHEMA_ACK_TIMEOUT`, session ends |
| iOS | anything valid from the receiver (PC) | `lease_ms` (10 s) | Session ends |
| iOS | complete HELLO on a new TCP connection | 5 s | Closes the connection |

- **Repair** = send SCHEMA_ACK for the stored table again (UDP only) and GET_SCHEMA with `schema_id = 0`.
- The FRAME timer starts when the first SCHEMA is stored and restarts with every new valid FRAME. SCHEMA and PONG do **not** restart it. A frame with the face lost still counts.
- For the iOS lease, a SCHEMA_ACK counts only when it acknowledges the current SCHEMA that iOS has already sent. An ACK for another `schema_id` is ignored and does not extend the lease.
- The sample also enters WAITING when nothing valid arrives for `lease_ms`, or when iOS sends ERROR for the session.
- **WAITING** is not an exit. Keep the port open and keep reading. Send nothing on a timer (no HELLO, PING, GET_SCHEMA or reconnect). Still answer a valid SCHEMA with SCHEMA_ACK. A valid FRAME returns to streaming. A manual start from iOS can begin a new session.
- While a session is streaming, a SCHEMA from another session is ignored: nobody can take over a running stream.

Receiver states used by the sample: `WAIT_SCHEMA` → `WAIT_FRAME` → `STREAMING` ⇄ `RECOVERING` → `WAITING`.

<a id="tcp"></a>
## 6. TCP (alternative)

TCP carries the same messages with these differences:

- **Normal start**: connect to the iOS TCP port 49984, send HELLO on that connection right after connecting, receive SCHEMA, then FRAMEs. **No SCHEMA_ACK.** iOS closes a connection on which no complete HELLO arrives within 5 seconds (Facemotion3d closes a connection on which no HELLO arrives within 2 seconds of connecting). In iFacialMocap and iFacialMocapTr, this iOS port also accepts the v1/v2 TCP commands, and iOS tells them apart by the first bytes. A connection that starts with `FMV3` is v3, and its 5 seconds count from when iOS accepted the connection. A connection that sends a v1/v2 command is handled as v1/v2 and is not subject to this limit. A connection on which nothing arrives, or only the beginning of `FMV3` (such as `F`), is closed after 5 seconds.
- **Manual start**: iOS connects to the receiver (PC) TCP port **49984** and sends SCHEMA, then FRAMEs, on that connection. Keep this listener open while a normal-start connection is running.
- **Message boundaries**: read 40 bytes, check the header, then read `chunk_length` bytes. Repeat. A read can contain part of a message or several messages. TCP messages are never split (`part_count = 1`), and there is no extra length prefix.
- **One connection, one session**: a session exists only on the connection that delivered its SCHEMA. A new connection starts with its own SCHEMA, even from the same address and port.
- **Errors**: when the stream is malformed, close that connection and keep listening. Do not search for the next `FMV3`.
- Recovery on a connection that stays open uses GET_SCHEMA only. PING is still sent every 3 s (for the reasons in [section 2](#udp)).

<a id="limits"></a>
## 7. Limits and validation

Check each message before using it. For UDP, drop an invalid datagram. For TCP, close the connection.

| Item | Rule |
|---|---|
| Header | Magic `FMV3`, header size 40, type 1–9, nonzero `session_id`, field rules of [3.2](#header) |
| HELLO body | Exactly 8 bytes |
| SCHEMA body | 20–262144 bytes (JSON ≤ 262124) |
| FRAME body | Exactly `N × 2` or `N × 4` + 48 bytes, at most 16432. All 12 floats finite |
| ERROR body | 1–512 bytes, valid UTF-8 |
| Other controls | 0 bytes |
| JSON | No duplicate keys, no `NaN`/`Infinity`, rules of [3.4](#schema) (names: unique as code point sequences, without normalization; no U+0000–U+001F) |
| Fragments | Up to 512 per message. Keep at most 8 incomplete messages and 524288 bytes. Drop an incomplete FRAME after 0.25 s and SCHEMA after 3 s |
| New session | Normal start: `client_nonce` equals your HELLO, and `actual_fps` and `max_udp_size` are not above your request. Manual start: `client_nonce = 0`, and `actual_fps` (1–60) and `max_udp_size` (576–1200) may be any valid value; iOS never received your settings, so do not compare these values with your local options. Only one incomplete new session at a time (3 s) |
| Old sessions | Ignore late messages of a session that was replaced (the sample remembers 32) |

A frame never skips validation, even when rendering is late.

<a id="compat"></a>
## 8. Compatibility

**Revision 5** (this document) differs from revision 4 (beta builds before the first App Store release of v3):

| | Revision 4 | Revision 5 |
|---|---|---|
| Message type numbers | HELLO 1, SCHEMA 3, SCHEMA_ACK 10 (2 unused) | **HELLO 1, SCHEMA 2, SCHEMA_ACK 3** (1–9 without a gap) |
| `contract_revision` | 4 | **5** |
| Receiver (PC) TCP port (manual start) | 49986 | **49984** |
| Facemotion3d iOS ports | UDP 49993 (and 49983), TCP 49994 | **UDP 49983, TCP 49984 (same as iFacialMocap)** |
| Unknown JSON keys / app names | rejected | **ignored / accepted** |

Different revisions do not work together. Update both sides together.

- A revision-5 app answers a revision-4 HELLO with `UNSUPPORTED_CONTRACT: requires 5`.
- A revision-5 receiver (PC) rejects the SCHEMA (type 3) that a revision-4 app sends in a manual start, with a message saying it looks like a revision-4 app.
- A revision-4 receiver (PC) ignores a revision-5 app's manual-start SCHEMA (type 2) as an unknown type. The app then reports an ACK timeout after 10 s.

v3 is an additional protocol. The v1/v2 text protocols of the apps are unchanged, and a v1/v2 receiver cannot read v3.

## Appendix: files in this repository

| File | Use |
|---|---|
| `face_motion_v3.py` | Reference receiver (UDP and TCP) and codec |
| `simulate_ios_v3.py` | Synthetic iOS sender for tests without a phone; not the real app |
| `golden_vectors.json` | Expected bytes for HELLO, SCHEMA_ACK, controls, SCHEMA and FRAME (UDP and TCP) |
| `diagrams/fmv3_flow.svg`, `diagrams/fmv3_messages.svg` | Overview pictures of the flows and the byte layout; this document is the definition |

Names in the Python sample: `Packet.kind` = `message_type`, `Packet.total_length` = `total_payload_length`, `len(Packet.payload)` = `chunk_length`.
