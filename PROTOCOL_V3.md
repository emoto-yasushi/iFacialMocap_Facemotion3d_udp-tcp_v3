# Face Motion v3 — Receiver specification

[日本語](PROTOCOL_V3_JA.md) · [README](README.md) · [Changes](CHANGELOG.md)

**Contract revision 6** (the version of this protocol). This document is for people who write a program that *receives* motion data from **iFacialMocap**, **iFacialMocapTr** or **Facemotion3d**. Below, the program that receives the data on a PC or an embedded device is called the "**receiver (PC)**", and the iPhone/iPad app that sends it is called "**iOS**".

Face Motion v3 (FMV3) uses binary messages (no JSON is used on the wire). iOS first sends a **SCHEMA** (the table) once, with the **mode** and **how to read the numbers** (for example, how many bytes one value uses). After that, every frame is a **FRAME** that carries only numbers, in a fixed order. The head and eye values have no names; they always follow the BlendShape numbers in a fixed order. The receiver (PC) therefore never parses text per frame; it just reads numbers by position. There are two modes, which differ in whether the BlendShape names are sent ([1.4](#modes)).

| | |
|---|---|
| Transport | **UDP** (recommended) or TCP |
| Byte order | Little-endian, no padding. A number of two or more bytes is written **lowest byte first** (for example, 1200 is `04B0` in hexadecimal, so it is sent as `b0 04`). There are **no empty bytes** between fields. A C struct copied as a whole may contain gaps added by the compiler, so read and write one field at a time |
| Message shape | Every message is "a 40-byte **header** + a **body** (payload)". The header is the common envelope of every message and holds its type and length. The **body (payload)** is the content that follows the header and is specific to the message (for a FRAME, the numbers). Some messages have no body |
| Security | None. Use a trusted LAN only |

Contents: [1 Basics](#basics) · [2 UDP step by step](#udp) · [3 Messages](#messages) ([3.4.4 Fixed52](#fixed52)) · [4 Table changes](#changes) · [5 Timers and recovery](#timers) · [6 TCP](#tcp) · [7 Limits and validation](#limits) · [8 Compatibility](#compat) · [Appendix A Byte examples](#examples) · [Appendix C Fixed52 names and indices](#fixed52-names)

Detailed rules and examples are folded. Click a "▶ Details: …" line to open it.

<a id="basics"></a>
## 1. Basics

### 1.1 Ports

Each side listens on exactly one port per transport. There are no fallback ports.

| App | iOS listens (UDP / TCP) | Receiver (PC) listens (UDP / TCP) |
|---|---|---|
| iFacialMocap, iFacialMocapTr, Facemotion3d ("Other" or "Unity" license) | 49983 / 49984 | 49983 / 49984 |

<details>
<summary>Details: Facemotion3d license and ports</summary>

Facemotion3d needs its "Other" or "Unity" license. With either license, v3 sends the "Other" output (the iFacialMocap-compatible output). v3 therefore uses the same ports as iFacialMocap, and the receiver (PC) does not need to know which app is sending.

</details>

- **Normal start**: the receiver (PC) knows the phone's address and contacts the **iOS port**.
- **Manual start**: the user types the address of the receiver (PC) into the iOS app, and iOS contacts the **receiver (PC) port**.

If a receiver (PC) port is already used by another program, report that clearly and stop. Do not pick another port on your own. The iOS apps always listen on the ports in the table; these cannot be changed. The receiver (PC) can change its own ports and the destination it contacts for a normal start (to reach an app, use the iOS ports in the table). In the iOS app, the user sets the destination of a manual start (the address and port of the receiver (PC)). If the receiver (PC) listens on another port, enter that port there.

<a id="terms"></a>
### 1.2 Terms

| Term | Meaning |
|---|---|
| Session | One stream, from the moment iOS starts sending until it stops. Each time iOS starts sending, it picks a number to tell this stream apart from others, `session_id` (a random nonzero 64-bit value), and puts it in the header of every message of that session. The receiver (PC) uses it to tell whether data belongs to the stream it is receiving, so that late data from an earlier stream is never mixed in |
| Mode | How the BlendShape values are sent: variable mode (`mode` = 0, default) or fixed52 mode (`mode` = 1) ([1.4](#modes)) |
| Datagram | One block of data sent at once over UDP |
| Fragment | One of the pieces when a large body is sent in several datagrams ([3.9](#fragments)) |
| Table (SCHEMA) | How to read the BlendShape numbers (mode, value type, count) and, in variable mode, the BlendShape name list. Within a session, tables are numbered by `schema_id` (1, 2, …). In variable mode the number grows each time the order of names or the value type (i16→i32) changes. In fixed52 mode it is always `schema_id = 1` |
| "Name → position" map | In variable mode, the map the receiver (PC) builds from the SCHEMA's name list. "Table" always means the SCHEMA; this map is a different thing |
| The standard 52 | The 52 BlendShapes defined by ARKit (the table in [Appendix C](#fixed52-names)). Fixed52 mode sends only these 52 |
| i16, i32 | The types of BlendShape values: i16 is a 2-byte and i32 a 4-byte signed integer (the same as Int16 and Int32) |
| `client_nonce` | A random nonzero 64-bit value the receiver (PC) picks for each normal start ("the HELLO nonce"). It is sent in HELLO, and iOS returns it unchanged in the SCHEMA start information, so the receiver (PC) can confirm that this SCHEMA answers its own HELLO. A value of 0 in SCHEMA means iOS started the stream (manual start) |
| `lease_ms` | The longest time iOS waits for word from the receiver (PC), in milliseconds. When nothing arrives for this long, iOS stops sending. Normally 10 s ([3.4.1](#start-info)) |
| Revision | `contract_revision`, the version of this protocol: **6**. The receiver (PC) and iOS must both support the same revision ([section 8](#compat)) |

<details>
<summary>Details: how client_nonce is used</summary>

A random nonzero 64-bit value the receiver (PC) picks for each normal start. "Nonce" means "a number used only once", and this value is also called "the HELLO nonce". The receiver (PC) puts it in the `session_id` field of the HELLO header, and iOS copies it unchanged into the SCHEMA start information ([3.4](#schema)). By checking that the SCHEMA's `client_nonce` equals the value in its own HELLO, the receiver (PC) confirms that this SCHEMA answers its current HELLO, and is not an old SCHEMA from an earlier run or one meant for another PC. A value of 0 in SCHEMA means iOS started the stream (manual start).

</details>

### 1.3 What a FRAME contains

- **BlendShape values**: signed integers (i16 or i32); in variable mode one per name of the SCHEMA, in the same order, and in fixed52 mode in the order of the table in [Appendix C](#fixed52-names). The unit is *percent*: `25` means 0.25, `-25` means −0.25 and `150` means 1.5. Values below 0 and above 100 are valid. Do not clamp them to 0–100.
- **Head**: rotation x, y, z in degrees and position x, y, z in meters (after the app's own scaling, axis settings and calibration).
- **Right eye** and **left eye**: rotation x, y, z in degrees.
- **State**: whether the face is tracked (tracking; also during playback, whether the camera tracks the face now) and whether a recording is being played back (playback), in the header's `flags` ([3.2](#header)).

These units and this order are fixed by this document, so they are not sent in SCHEMA. The byte positions are in the table of [3.6](#frame).

<a id="modes"></a>
### 1.4 How BlendShape values are sent (two modes)

There are two ways to send the BlendShape values (two **modes**). They differ in whether the BlendShape names are sent.

| | Variable mode (`mode` = 0, default) | Fixed52 mode (`mode` = 1) |
|---|---|---|
| BlendShapes | From 0 to 4096, depending on the app and its settings (custom BlendShapes, voice-input settings and so on). The names are sent in SCHEMA | Only the standard 52 of ARKit ([Appendix C](#fixed52-names)). The names are not sent; a fixed order is used |
| Value type | i16 (2 bytes) or i32 (4 bytes), announced in SCHEMA | Always i32 |
| List changes during a stream | Normally none. But the list can change, for example when playback starts of a recording with another BlendShape list; a new SCHEMA then arrives ([section 4](#changes)) | None (`schema_id` is always 1) |
| What the receiver (PC) must implement | A "name → position" map, reassembly of fragments ([3.9](#fragments)), switching tables ([section 4](#changes)) | Nothing extra; it reads fixed positions |

<details>
<summary>Details: SCHEMA and FRAME sizes</summary>

| | Variable mode | Fixed52 mode |
|---|---|---|
| SCHEMA size | Body of 30 bytes or more. Even the standard 52 names alone are normally split into 2 fragments over UDP ([3.9](#fragments)) | Body of 26 bytes (66 bytes in total). Never split |
| FRAME size | 40 + (2 or 4) × N + 48 bytes (N is the number of names) | Always 296 bytes |

</details>

Both modes need: validating the SCHEMA; over UDP, SCHEMA_ACK (the reply "received", [3.5](#schema-ack)); PING every 3 s over both UDP and TCP ([section 2](#udp)); skipping old frames by `sequence`; and recovery ([section 5](#timers)).

**How the mode is chosen.** In a normal start the receiver (PC) chooses it in HELLO; in a manual start it is chosen in the iOS app's settings. The mode never changes during a session.

<details>
<summary>Details: normal start, manual start and how the mode is chosen</summary>

A normal start is when the receiver (PC) starts by sending HELLO; the receiver (PC) then chooses the mode with `requested_mode` in HELLO. A manual start is when the address of the receiver (PC) is set in the iOS app and iOS starts sending; the mode is then chosen in the iOS app's settings (where to find them: "Manual start" in [section 2](#udp)). The mode never changes during a session (one stream from start to end, [1.2](#terms)). To change it, end the session and start a new one ([3.3](#hello)).

</details>

**If you accept only fixed52**, you can skip 3.4.3, 3.9 and section 4 (except "Sending SCHEMA again" at its end). What you need is collected in the checklist at the end of [3.4.4](#fixed52). If you support TCP, also read section 6.

<a id="udp"></a>
## 2. UDP step by step

```text
Receiver (PC)                                   iOS
  bind UDP 49983
  |---- HELLO (1) ------------------------------->|  to iOS UDP 49983
  |<--- SCHEMA (2) -------------------------------|  variable: usually about 2 datagrams; fixed52: 1 datagram
  |     reassemble, validate, store               |
  |---- SCHEMA_ACK (3) --------------------------->|
  |<--- FRAME (4) --------------------------------|  repeated, up to the agreed FPS
  |---- PING (6) every 3 s ----------------------->|
  |<--- PONG (7) ---------------------------------|
  |---- STOP (8) when the receiver (PC) quits ---->|
```

The numbers in brackets are message type numbers ([3.1](#types)).

1. **Bind** a UDP socket to the receiver (PC) port (assign the port number to the socket, so that it receives what arrives on that port). Use this one socket for everything: iOS replies to the address and port the receiver (PC) sends from.
2. Pick a new random `client_nonce` (the number of this HELLO, [1.2](#terms)) and **send HELLO**. If no SCHEMA arrives, send the same HELLO again every second, 5 times in total.
3. **Receive SCHEMA.** It may arrive in several datagrams (fragments, [3.9](#fragments)). After joining them, check that `client_nonce` equals your own value, that the descriptor (the 6 bytes that give the mode, value type and count, [3.4.2](#descriptor)) is valid and that the mode is the one you requested in HELLO, then store the table.
4. **Send SCHEMA_ACK** for that `schema_id`. iOS sends no FRAME before this ACK.
5. **Receive FRAMEs.** Use the stored table to read the numbers. Skip frames that are older than the last one you used ([3.6](#frame)).
6. While the session runs, **the receiver (PC) sends PING to iOS every 3 seconds**. When iOS hears nothing from the receiver (PC) for `lease_ms` (normally 10 s), it decides the receiver (PC) is gone and stops sending.
7. **Send STOP** when the receiver (PC) quits on purpose.

<details>
<summary>Details: why PING is needed</summary>

UDP has no "connection". If the receiver (PC) program quits or the PC leaves the network, iOS cannot notice. Without PING, iOS would keep sending to nobody and keep using battery and network. A sending iOS also refuses a start from another receiver (`BUSY`), so nobody could start again from another PC. The rule "the receiver (PC) sends PING regularly, and iOS stops by itself when PINGs stop" prevents this. The PONG that iOS returns also tells the receiver (PC) that iOS is still running, including while FRAMEs pause (for example, during a table change). TCP uses the same rule.

</details>

**When iOS refuses to start.** When iOS refuses a HELLO, it answers with ERROR instead of SCHEMA. Show the received text to the user (full list of reasons in [3.8](#error)).

<details>
<summary>Details: main reasons for a refusal, and v1/v2 streaming</summary>

This happens mainly when iOS is already streaming to another receiver, when the license or trial time does not allow streaming now, when the app is not on its tracking screen, when "Settings → Other functions → No connection accepted from PC" is on in Facemotion3d, or when the HELLO has another revision. v1/v2 streaming that a PC handshake started (the request a v1/v2 receiver program sends to iOS to ask it to start sending) is an exception: a v3 HELLO replaces it instead of getting `BUSY` (in Facemotion3d, only streaming to the software name "Other"; 3.8.1 in [3.8](#error)).

</details>

**When iOS stops sending.** When sending stops on the iOS side, no message tells the receiver (PC) (STOP goes from the receiver (PC) to iOS; only when the stream ends with an error does an ERROR arrive). Over UDP the FRAMEs simply stop, so the receiver (PC) tries to recover as in [section 5](#timers) and then waits (WAITING). Over TCP the connection closes, so the receiver (PC) waits right away.

<details>
<summary>Details: when iOS stops sending</summary>

For example, when the user presses Stop in the app, leaves the tracking screen, the trial time ends, or the camera is interrupted (for example, another app starts using it) or fails. While the camera is interrupted, iOS does not answer a HELLO (over UDP nothing comes back; over TCP the connection fails or is closed without an ERROR). When the interruption ends, streaming that was started on iOS (in Facemotion3d, with the Live button) starts again by itself as a new session. Streaming that a receiver (PC) started does not start again. The receiver (PC) is then waiting (WAITING), so start it again. In Facemotion3d with neither the "Other" nor the "Unity" license, iOS does not refuse the HELLO: it starts sending and stops by itself after about 10 seconds.

</details>

**Manual start (UDP).** The receiver (PC) only binds its port and waits. iOS sends SCHEMA with `client_nonce = 0`. The receiver (PC) stores it and sends SCHEMA_ACK to the sender's address. Then FRAMEs follow. Each time iOS starts sending, a new session begins. The mode of a manual start is chosen in the iOS app's settings.

When a SCHEMA arrives in a mode that the receiver (PC) does not accept, the receiver (PC) rejects that SCHEMA and sends no SCHEMA_ACK (over TCP it closes the connection). Over UDP, the app then ends the stream after 10 s with `SCHEMA_ACK_TIMEOUT`. The cause is hard to see in the app, so the receiver (PC) should tell the user that the modes differ, for example "The app's mode (variable/fixed52) differs from the receiver's setting". The reference receiver accepts only its configured mode (`--mode variable` by default, or `--mode fixed52`).

<details>
<summary>Details: settings in the iOS app (destination, transport, mode)</summary>

For a manual start, set up the iOS app as follows. The mode setting is used only for manual start (in a normal start, the mode is the one the receiver (PC) chose in HELLO).

| App | Destination and transport | Mode | When sending starts |
|---|---|---|---|
| iFacialMocap, iFacialMocapTr | Settings → enter the address of the receiver (PC) in "Destination IP address" in the "Destination setting" section; in "Detail Settings" of the same section, set "Protocol" to "Face Motion v3 UDP" or "Face Motion v3 TCP/IP" | In the same "Detail Settings", choose "Variable (default)" or "Fixed 52 (ARKit)" in "Face Motion v3 mode" | When the tracking screen is shown (it starts by itself) |
| Facemotion3d | Settings → "Streaming settings": set "Software Name" to "Other", "Sending Format" to "Face Motion v3" and "Communication Method" to "UDP" or "TCP/IP" | Press the button at the top right of the same screen ("v3: Variable" or "v3: ARKit 52") and choose "Variable BlendShapes" (variable) or "ARKit 52 (Fixed)" (fixed52) | When you press the Live button, choose "Other", enter the IP address of the receiver (PC) and press "Connect" |

</details>

<details>
<summary>Details: a manual-start SCHEMA that arrives while you wait for the answer to your HELLO</summary>

While the receiver (PC) is waiting for the answer to its HELLO in a normal start, a SCHEMA with `client_nonce = 0` may arrive, because sending was started on the iOS side. Accept this SCHEMA too, as a manual start. After accepting it, stop resending the HELLO. The reference receiver accepts it only from the IP address of the iPhone/iPad that it sent the HELLO to (from any source port).

When this happens, iOS refuses the HELLO with an ERROR (usually `BUSY`). This ERROR often arrives before the manual-start SCHEMA. If it does, enter WAITING ([section 5](#timers)), keep the port open, and accept the manual-start SCHEMA that arrives later. Ignore an ERROR that arrives after you accepted the manual-start SCHEMA ([3.8](#error)). Also ignore a SCHEMA that answers that HELLO while the manual-start session is streaming.

</details>

<a id="messages"></a>
## 3. Messages

<a id="types"></a>
### 3.1 Message types

The **message type number** is one byte that says what a message is. It is the fifth byte of the header (`message_type` at offset 4). Look at it first to decide how to read the rest. The numbers follow the order of a normal UDP start (HELLO → SCHEMA → SCHEMA_ACK → FRAME).

| Type | Name | Direction | Body | Purpose |
|---:|---|---|---|---|
| 1 | HELLO | receiver (PC) → iOS | 10 bytes | Asks iOS to start sending |
| 2 | SCHEMA | iOS → receiver (PC) | 26 bytes + variable-mode name list | Mode, how to read the numbers, session settings; the name list only in variable mode |
| 3 | SCHEMA_ACK | receiver (PC) → iOS | none | "The whole SCHEMA arrived and is stored" (UDP only) |
| 4 | FRAME | iOS → receiver (PC) | numbers | The values of one frame |
| 5 | GET_SCHEMA | receiver (PC) → iOS | none | Asks iOS to send the SCHEMA again |
| 6 | PING | receiver (PC) → iOS | none | "The receiver (PC) is still receiving" |
| 7 | PONG | iOS → receiver (PC) | none | Answer to PING |
| 8 | STOP | receiver (PC) → iOS | none | Asks iOS to end the session |
| 9 | ERROR | iOS → receiver (PC) | UTF-8 text | Reason for a refused start or a session error |

Any other type number is invalid.

<a id="header"></a>
### 3.2 Header (40 bytes)

The 40 bytes at the start of every message. Python can read and write it with `struct.Struct("<4sBBHQIIIIHHI")`.

<a id="formats"></a>

<details>
<summary>Details: how to read format strings (Python struct formats)</summary>

A string such as `<4sBBHQIIIIHHI` is a format of Python's `struct` module that describes a sequence of bytes. From left to right, each letter (`4s`: two characters) stands for one value. In other languages, read and write one field at a time using the offsets and types in the tables.

| Format letter | Meaning | Bytes |
|---|---|---:|
| `<` | Little-endian, no padding (no gaps between values) | — |
| `4s` | 4-byte string | 4 |
| `B` | UInt8 (unsigned integer) | 1 |
| `H` | UInt16 (unsigned integer) | 2 |
| `I` | UInt32 (unsigned integer) | 4 |
| `Q` | UInt64 (unsigned integer) | 8 |
| `h` / `i` | Int16 / Int32 (signed integer; i16 and i32 in this document, the FRAME BlendShape values) | 2 / 4 |
| `f` | Float32 (decimal; FRAME head and eye values) | 4 |

For example, the HELLO body `<HHIH` means "UInt16, UInt16, UInt32, UInt16 in this order, 10 bytes in total", the SCHEMA start information `<QHHII` means "UInt64, UInt16, UInt16, UInt32, UInt32 in this order, 20 bytes in total", and the SCHEMA descriptor `<HBBH` means "UInt16, UInt8, UInt8, UInt16 in this order, 6 bytes in total".

</details>

The header fields:

| Offset | Size | Field | Content |
|---:|---:|---|---|
| 0 | 4 | `magic` | ASCII `FMV3`, the mark of an FMV3 message |
| 4 | 1 | `message_type` | The message type number of [3.1](#types) |
| 5 | 1 | `flags` | FRAME state (table below). 0 on other messages |
| 6 | 2 | `header_size` | Always 40 |
| 8 | 8 | `session_id` | The session number ([1.2](#terms)). Nonzero |
| 16 | 4 | `schema_id` | The number of the table this message uses (below) |
| 20 | 4 | `sequence` | A counter (below) |
| 24 | 4 | `source_token` | FRAME only ([3.6](#frame)). Otherwise 0 |
| 28 | 4 | `total_payload_length` | Size of the whole body in bytes (before splitting) |
| 32 | 2 | `part_index` | Which fragment this is when split, counted from 0 (below) |
| 34 | 2 | `part_count` | How many fragments the body was split into (below) |
| 36 | 4 | `chunk_length` | Body bytes in this datagram / message |

**`flags`** (FRAME state): bit 0 (value 1) is tracking: whether the face is tracked. Bit 1 (value 2) is playback: whether the values come from a recording being played back (0 means live camera values). The other bits are not used (always 0).

| `flags` | playback (bit 1) | tracking (bit 0) | Meaning |
|---:|---:|---:|---|
| 1 | 0 | 1 | Live; the camera tracks the face now |
| 0 | 0 | 0 | Live; the face is lost |
| 3 | 1 | 1 | Playback; the camera tracks the face now |
| 2 | 1 | 0 | Playback; the face is lost now |

<details>
<summary>Details: tracking during playback</summary>

During playback, tracking also shows whether the camera tracks the face now, because none of the apps stores the tracking state in its recordings.

</details>

- **`session_id`**: which session the message belongs to. iOS picks it when it starts sending and tells the receiver (PC) in the SCHEMA header. After that, iOS and the receiver (PC) put this same number in every message of the session. When a HELLO is sent there is no session yet (no number has been picked), so a HELLO carries the `client_nonce` picked by the receiver (PC) instead.
- **`schema_id`**: the number of a table (SCHEMA). In variable mode the table can change even within one session (for example when playback starts of a recording with another BlendShape list; [section 4](#changes)). So every message that uses a table carries the number of that table: on a SCHEMA the number of the table itself, on SCHEMA_ACK the table that was received and stored, on a FRAME the table to read the numbers with, and on GET_SCHEMA the table to send again (see the example below). In fixed52 mode the table never changes, and the number is always 1.
- **`sequence`**: on a FRAME, a counter that grows by 1 per frame; the receiver (PC) uses it to spot old and duplicate frames. On PING/PONG, it pairs an answer with its PING.
- **`part_index`, `part_count`, `chunk_length`**: over UDP, a body that does not fit into one datagram is sent in several fragments. `part_count` is the number of fragments, `part_index` is which fragment this is (from 0), and `chunk_length` is the body bytes in this fragment. Without splitting: `part_index = 0`, `part_count = 1`, `chunk_length = total_payload_length` (`part_count` is 1, not 0).
  - **SCHEMA** in variable mode is **normally split** over UDP (why, with an example: [3.9](#fragments)). A receiver (PC) that accepts variable mode must at least implement SCHEMA reassembly.
  - **FRAME** is normally not split.
  - **TCP** never splits (`part_count = 1` always).

<details>
<summary>Details: values per type (session_id, schema_id, sequence)</summary>

| Type | Value of `session_id` | Value of `schema_id` | Value of `sequence` |
|---|---|---|---|
| HELLO | the `client_nonce` picked by the receiver (PC) (there is no session yet) | 0 (unused) | 0 (unused) |
| SCHEMA | this session's `session_id`, picked by iOS | variable: starts at 1 and grows by 1 when names, order or value type change; fixed52: always 1 | 0 (unused) |
| SCHEMA_ACK | the same `session_id` as the received SCHEMA | the `schema_id` of the SCHEMA that was received and stored | 0 (unused) |
| FRAME | this session's `session_id` | which table to read these numbers with (that table's `schema_id`) | the frame counter |
| GET_SCHEMA | this session's `session_id` | the `schema_id` of the table to send again; 0 = the newest table | 0 (unused) |
| PING | this session's `session_id` | 0 (unused) | a number the receiver (PC) picks (1, 2, 3, …) |
| PONG | this session's `session_id` | 0 (unused) | the same number as the received PING |
| STOP | this session's `session_id` | 0 (unused) | 0 (unused) |
| ERROR | this session's `session_id`, or the HELLO's `client_nonce` when refusing a HELLO | 0 (unused) | 0 (unused; other values may be ignored) |

</details>

<details>
<summary>Details: how schema_id is used in variable mode (UDP)</summary>

```text
iOS -> receiver (PC)   SCHEMA      schema_id=1  blend_names = [eyeBlink_L, jawOpen, mouthSmile_L]
receiver (PC) -> iOS   SCHEMA_ACK  schema_id=1   <- table 1 is stored
iOS -> receiver (PC)   FRAME       schema_id=1  numbers = [10, 45, 3]      <- 3 values in table-1 order
    (for example, playback starts of a recording that has one more BlendShape)
iOS -> receiver (PC)   SCHEMA      schema_id=2  blend_names = [eyeBlink_L, jawOpen, mouthSmile_L, myCustomSmile]
receiver (PC) -> iOS   SCHEMA_ACK  schema_id=2   <- table 2 is stored; iOS now sends in table-2 order
iOS -> receiver (PC)   FRAME       schema_id=2  numbers = [10, 45, 3, 80]  <- 4 values in table-2 order
```

A FRAME's `schema_id` tells the receiver (PC) which table to read its numbers with, so frames sent around a table change are never mixed up. The `schema_id` in SCHEMA_ACK lets iOS confirm which table reached the receiver (PC).

</details>

<a id="hello"></a>
### 3.3 HELLO

40-byte header + 10-byte body, 50 bytes in total. The body format is `<HHIH` (UInt16, UInt16, UInt32, UInt16 in this order; [how to read formats](#formats)):

| Body offset | Type | Field | Value |
|---:|---|---|---|
| 0 | UInt16 | `requested_fps` | 1–60, the highest frame rate the receiver (PC) wants |
| 2 | UInt16 | `max_udp_size` | 576–1200, largest FRAME datagram including the 40-byte header |
| 4 | UInt32 | `contract_revision` | 6 |
| 8 | UInt16 | `requested_mode` | `0` = variable, `1` = fixed52. For any other value, iOS answers with `UNSUPPORTED_MODE` |

Examples for 60 fps and 1200 bytes: variable `3c 00 b0 04 06 00 00 00 00 00`; fixed52 `3c 00 b0 04 06 00 00 00 01 00`. An example that includes the header is in [Appendix A](#examples).

<details>
<summary>Details: changing the mode</summary>

iOS streams in the requested mode and never switches to another mode. To change the mode, the receiver (PC) sends STOP to end the session and then sends HELLO with a new `client_nonce` (a HELLO with another nonce sent without STOP gets `BUSY`; a HELLO with the same nonce and different settings gets `HELLO_SETTINGS_CHANGED`).

</details>

The `session_id` field of the HELLO header carries the `client_nonce` (the HELLO nonce, [1.2](#terms)) that the receiver (PC) picked for this normal start. iOS returns this value in the SCHEMA start information, so the receiver (PC) can confirm that the SCHEMA answers its own HELLO.

Repeat an unanswered HELLO unchanged (same nonce, same body). If the same HELLO reaches a running session, iOS only sends its SCHEMA again; the session is not restarted.

<a id="schema"></a>
### 3.4 SCHEMA

SCHEMA is the message in which iOS tells how to read the FRAME numbers. Its body consists of these three parts, in this order, with no gaps.

| Part | Start information | Descriptor | Name list |
|---|---|---|---|
| Content | Session settings ([3.4.1](#start-info)) | Mode, value type, SCHEMA format version, number of BlendShapes ([3.4.2](#descriptor)) | App name, output type name, BlendShape names ([3.4.3](#variable-names)); variable mode only |
| Bytes | 20 | 6 | Variable: 4 or more (depends on the string lengths). Fixed52: 0 |

The fixed52 body is exactly **26 bytes** (66 bytes including the header).

**Example: a variable-mode SCHEMA body with 3 names (98 bytes).** These are the same bytes as A.3 in Appendix A. `"…"` stands for the UTF-8 bytes of that string.

```text
11 22 33 44 55 66 77 88 1e 00 bc 02 10 27 00 00 06 00 00 00   start information
00 00 01 01 03 00                                             descriptor: variable, i16, version 1, 3 names
0c 00 "Facemotion3d"                                          app name (12 bytes)
12 00 "facemotion3d-other"                                    output type name (18 bytes)
07 "jawOpen"                                                  name 1 (7 bytes)
0f "独自の笑顔"                                               name 2 (15 bytes)
0d "myCustomSmile"                                            name 3 (13 bytes)
```

<details>
<summary>Details: how to read these bytes, one by one</summary>

Two hexadecimal characters make one byte (a number from 0 to 255): for example `1e` is 30, `0c` is 12 and `12` is 18. A number of two or more bytes has its lowest byte first, so `bc 02` is read as `02bc`, which is 700.

| Bytes | How to read them | Meaning |
|---|---|---|
| `11 22 33 44 55 66 77 88` | 8 bytes make one number | `client_nonce`: the value the receiver (PC) sent in HELLO, returned unchanged |
| `1e 00` | `001e` → 30 | `actual_fps`: at most 30 fps |
| `bc 02` | `02bc` → 700 | `max_udp_size`: 700 bytes |
| `10 27 00 00` | `00002710` → 10000 | `lease_ms`: 10000 milliseconds (10 s) |
| `06 00 00 00` | 6 | `contract_revision`: revision 6 |
| `00 00` | 0 | `mode`: variable mode |
| `01` | 1 | `blend_encoding`: i16 (2 bytes per value) |
| `01` | 1 | `schema_format_version`: 1 |
| `03 00` | 3 | `blend_count`: 3 names |
| `0c 00` | `000c` → 12 | The next 12 bytes are the app name |
| `46 61 63 65 6d 6f 74 69 6f 6e 33 64` | characters (`46` = F, `61` = a, …) | `Facemotion3d` |
| `12 00` | `0012` → 18 (not decimal 12) | The next 18 bytes are the output type name |
| `66 61 63 65 6d 6f 74 69 6f 6e 33 64 2d 6f 74 68 65 72` | characters | `facemotion3d-other` |
| `07` | 7 | The next 7 bytes are the first name |
| `6a 61 77 4f 70 65 6e` | characters | `jawOpen` (the name of the first FRAME value) |
| `0f` | 15 | The next 15 bytes are the second name |
| `e7 8b ac e8 87 aa e3 81 ae e7 ac 91 e9 a1 94` | a Japanese character takes 3 bytes | `独自の笑顔` (second) |
| `0d` | 13 | The next 13 bytes are the third name |
| `6d 79 43 75 73 74 6f 6d 53 6d 69 6c 65` | characters | `myCustomSmile` (third). The body ends here, at exactly 98 bytes |

</details>

<a id="start-info"></a>
#### 3.4.1 SCHEMA start information (the first 20 bytes of the body)

The first 20 bytes of the SCHEMA body hold the session settings. The format is `<QHHII` (UInt64, UInt16, UInt16, UInt32, UInt32 in this order; [how to read formats](#formats)). These values stay the same for the whole session.

| Body offset | Type | Field | Meaning |
|---:|---|---|---|
| 0 | UInt64 | `client_nonce` | Normal start: the `client_nonce` sent in HELLO (the HELLO nonce), returned unchanged. The receiver (PC) checks that it equals its own value. Manual start: 0, because there is no HELLO |
| 8 | UInt16 | `actual_fps` | The highest frame rate iOS will actually use. Normal start: 1 up to `requested_fps`. Manual start: 1–60. iOS never sends FRAMEs faster than this, also right after SCHEMA_ACK and at the start and end of playback (to catch up with timer lateness, one interval can be up to 2 ms shorter) |
| 10 | UInt16 | `max_udp_size` | Largest FRAME datagram including the header. Normal start: 576 up to the requested value. Manual start: 576–1200 |
| 12 | UInt32 | `lease_ms` | How long iOS waits for anything (such as PING) from the receiver (PC), in milliseconds. 5000–60000, normally 10000 (10 s). When nothing arrives for this long, iOS decides the receiver (PC) is gone and ends the session |
| 16 | UInt32 | `contract_revision` | 6 |

<a id="descriptor"></a>
#### 3.4.2 SCHEMA descriptor (the 6 bytes after the start information, both modes)

These are the 6 bytes of the SCHEMA body right after the start information. They give the mode and how to read the FRAME numbers. Its format is `<HBBH` (UInt16, UInt8, UInt8, UInt16 in this order), and offsets are counted from the beginning of the SCHEMA body.

| Body offset | Type | Field | Meaning |
|---:|---|---|---|
| 20 | UInt16 | `mode` | `0` = variable, `1` = fixed52 |
| 22 | UInt8 | `blend_encoding` | The type of one BlendShape value: `1` = i16 (2-byte signed integer, −32768 to 32767), `2` = i32 (4-byte signed integer). Fixed52 allows only `2` |
| 23 | UInt8 | `schema_format_version` | The version of how SCHEMA is written. Always `1` |
| 24 | UInt16 | `blend_count` | The number of BlendShapes, which is the number `N` of values in a FRAME. Variable: 0–4096. Fixed52: always 52 |

Examples: variable mode with 4 names as i16 is `00 00 01 01 04 00`; fixed52 is `01 00 02 01 34 00`.

The receiver (PC) rejects a SCHEMA whose mode or value type is not in the table, and a SCHEMA whose `schema_format_version` is not 1 (that means the layout of the bytes from the descriptor onward has changed). In a normal start, it checks that `mode` equals the HELLO's `requested_mode`. In a manual start the iOS app's setting decides the mode, so it checks that it accepts that mode (what happens if it does not: "Manual start" in [section 2](#udp)).

<a id="variable-names"></a>
#### 3.4.3 SCHEMA name list (after the descriptor, variable mode only)

This is the last part of the SCHEMA body, present only in variable mode. Strings follow right after the descriptor. Every string is written as **"length + string"**, packed together with no separator and no terminator.

1. App name (`app`): length (UInt16, 2 bytes) + UTF-8 string. For example `iFacialMocap`, `Facemotion3d`
2. Output type name (`profile`): length (UInt16, 2 bytes) + UTF-8 string. For example `facemotion3d-other`
3. BlendShape names (`blend_names`): length (UInt8, 1 byte) + UTF-8 string, repeated `blend_count` times

With each length written in `[ ]`, the strings of the example in 3.4 look like this:

```text
[0c 00]Facemotion3d[12 00]facemotion3d-other[07]jawOpen[0f]独自の笑顔[0d]myCustomSmile
```

The value in `[ ]` is the byte count of the string right after it, in hexadecimal (`0c` = 12, `0f` = 15). It is **not a separator**, so it differs from name to name. The FRAME numbers follow the order of these names (in this example, the value of `jawOpen` comes first).

- Lengths count bytes, not characters (`独自の笑顔`, Japanese for "custom smile", is 5 characters but 15 bytes).
- There are 0–4096 names, without duplicates, each 1–255 bytes.
- `app` and `profile` are for display and logs. Accept new values and empty strings.
- Check that the body ends right after the last name. Reject truncated fields, invalid UTF-8 and extra bytes at the end.

<details>
<summary>Details: comparing names, allowed characters, SCHEMA size limit</summary>

Names are compared as they are, without Unicode normalization: two names are the same only when their sequences of Unicode code points (in other words, their UTF-8 bytes) are equal. For example, `é` written as U+00E9 and `é` written as U+0065 U+0301 are two different names, even though they look the same. This rule applies to the duplicate check, to the "name → position" map and to deciding whether a name changed ([section 4](#changes)). The forbidden control characters are exactly U+0000–U+001F. All other characters, including U+007F and U+0080–U+009F, are allowed.

Besides the limit of each field, keep the 262144-byte limit for the whole SCHEMA body. The smallest variable-mode SCHEMA body is 30 bytes (start information 20 + descriptor 6 + the lengths of an empty `app` and `profile`, 2 + 2).

</details>

<details>
<summary>Details: how the standard 52 names are spelled (eyeBlink_L), and app-specific names</summary>

Every app sends the standard 52 BlendShape names in the "String returned by ARKit" spelling (the "String returned by ARKit" column of the table in [Appendix C](#fixed52-names), for example `eyeBlink_L` and `mouthSmile_R`). This is the same spelling as iFacialMocap's v1/v2. Some names, such as `jawOpen`, `jawLeft` and `mouthLeft`, are the same as the identifier.

Besides the standard 52, Facemotion3d also sends `FM_SAD`, `FM_ANGRY`, `FM_VF`, `FM_TH`, `FM_browUpRight` and `FM_browUpLeft`, and the names registered in its voice-input settings. When its name-replacement setting is used, it sends the replaced names.

</details>

<a id="fixed52"></a>
#### 3.4.4 Fixed52 mode: SCHEMA and FRAME

Fixed52 SCHEMA has only the start information and the descriptor: **no app name, no output type name and no BlendShape names**. A FRAME carries the values of ARKit's standard 52 BlendShapes, always as **Int32**, in a fixed order. The order is the alphabetical order of the ARKit identifiers, and the value with index `k` (0–51) starts at byte `4 × k` of the FRAME body. A receiver (PC) that needs the names builds them from the table in [Appendix C](#fixed52-names).

- **Order**: use only the order of the table in [Appendix C](#fixed52-names), which this document fixes. Do not use a dictionary's iteration order or an order built at runtime.
- **Missing and extra names**: a name that is missing from the live or recorded data is sent as 0. Custom BlendShapes that are not in the table are not sent. Name-replacement settings do not change the indices or positions.
- **`schema_id` and type**: `schema_id = 1` and type i32 for the whole session; they never change.

<details>
<summary>Details: the two spellings, how values are made, what never changes</summary>

- **The two name columns** (table in [Appendix C](#fixed52-names)): "Name" is the ARKit Swift identifier (for example `ARFaceAnchor.BlendShapeLocation.eyeBlinkLeft`), and "String returned by ARKit" is its value (`rawValue`). Use them to match names from variable mode or v1/v2 (variable mode sends the "String returned by ARKit" spelling: [3.4.3](#variable-names)). iOS puts a name in either spelling into the same index.
- **Values**: integer percent, as in variable mode (−25 and 150 also occur). They include the app's numeric adjustments, scaling, calibration and recording/playback processing; they are not ARKit's own decimal values (Float32).
- **What never changes**: playback, missing names, custom names and values outside the i16 range change neither the layout nor the type, and no new table is made. A different fixed52 SCHEMA, a `schema_id` other than 1 or a mode change within the same session is invalid.

</details>

**Checklist for a receiver (PC) that accepts only fixed52:**

| | Content |
|---|---|
| What you can skip | Parsing names, the "name → position" map, reassembly of fragments ([3.9](#fragments)), switching tables ([section 4](#changes)) |
| Check in SCHEMA | The body is exactly 26 bytes, `part_count = 1`, `schema_id = 1`, `session_id` is nonzero. In the start information: `client_nonce` is the HELLO value in a normal start and 0 in a manual start, `contract_revision` is 6, and `actual_fps`, `max_udp_size` and `lease_ms` are in the ranges of [section 7](#limits). The descriptor is `01 00 02 01 34 00` ([3.4.2](#descriptor)) |
| Check in FRAME | The body is exactly 256 bytes, `session_id` is the current session's, `schema_id = 1`, `part_count = 1`, `sequence` is newer, all 12 Float32 values are finite ([3.6](#frame)) |
| Do as in variable mode | SCHEMA_ACK over UDP, PING every 3 s, `lease_ms`, recovery ([section 5](#timers)), sending SCHEMA_ACK again when a stored SCHEMA arrives again (end of [section 4](#changes)) |

<a id="schema-ack"></a>
### 3.5 SCHEMA_ACK

40 bytes with no body: type number 3, the SCHEMA's `session_id` and the stored `schema_id`. Send it only after **every** fragment has arrived and the whole table is valid and stored. Only UDP uses it.

Example: `46 4d 56 33 03 00 28 00 08 07 06 05 04 03 02 01 01 00 00 00 …` (the rest is zero except `part_count = 1`; the full 40 bytes are in A.2 of [Appendix A](#examples)).

<a id="frame"></a>
### 3.6 FRAME

A FRAME contains these parts, from left to right.

| Part | Header | BlendShape values | Head | Right eye | Left eye |
|---|---|---|---|---|---|
| Content | Common header ([3.2](#header)) | variable: N values in `blend_names` order; fixed52: 52 values in the order of [3.4.4](#fixed52) | rotation rx, ry, rz; position px, py, pz | rotation rx, ry, rz | rotation rx, ry, rz |
| Type | — | variable: i16 or i32; fixed52: i32 | Float32 × 6 | Float32 × 3 | Float32 × 3 |
| Bytes | 40 | i16: 2 × N, i32: 4 × N | 24 | 12 | 12 |

`N` is the descriptor's `blend_count`. The head and both eyes are 12 Float32 values, 48 bytes together (format `<12f`). In variable mode, 52 names as i16 take 40 + 104 + 48 = **192 bytes**.

A **fixed52 FRAME** is always **296 bytes** (256-byte body), with the parts at these positions.

| Part | Header | 52 BlendShapes | Head | Right eye | Left eye |
|---|---|---|---|---|---|
| Position from the start of the message | 0 | 40 | 248 | 272 | 284 |
| Position from the start of the body | — | 0 | 208 | 232 | 244 |
| Bytes | 40 | 208 (Int32 × 52) | 24 | 12 | 12 |

- `sequence` counts frames in the session. A frame is newer when `1 ≤ (new − last) mod 2³² ≤ 2³¹ − 1` (new is the received number, last is the last one used). The first FRAME of a session is accepted whatever its number. Skip duplicates and older frames. Gaps are normal: iOS drops frames it could not send in time and never resends them.
- (Variable mode only) A FRAME for a `schema_id` you do not have yet: skip it and send GET_SCHEMA (at most once per second).
- (Variable mode only) A FRAME for an older `schema_id`: skip it. Never read it with the newer table.
- (Fixed52 only) A FRAME whose `schema_id` is not 1, or whose `part_count` is not 1, is invalid ([section 7](#limits)).
- Face lost: bit 0 of `flags` is 0 (during playback too, this is the current camera state; [3.2](#header)). The numbers are still a valid frame. iOS does not wait for a face before it sends SCHEMA and FRAMEs: until it first finds a face, it keeps sending FRAMEs with bit 0 set to 0.
- `source_token`: a UInt32 the app sets for its own use. It is not part of the table and not authentication. `0` means "no value". The receiver (PC) may ignore it.

### 3.7 GET_SCHEMA, PING, PONG, STOP

Header only (body 0 bytes).

- **GET_SCHEMA**: "please send table `schema_id` (0 = the newest) again". iOS sends the newest table.
- **PING / PONG**: the receiver (PC) tells iOS that it is still receiving, and checks that iOS is still running (why: [section 2](#udp)). iOS copies `sequence` into PONG.
- **STOP**: ends the session. Send it when the receiver (PC) quits on purpose, not because retries ran out.

<a id="error"></a>
### 3.8 ERROR

UTF-8 text, 1–512 bytes. Discard an ERROR whose text is not valid UTF-8.

<details>
<summary>Details: the session_id field of an ERROR header</summary>

In an ERROR that refuses a HELLO, the `session_id` field of the header carries that HELLO's `client_nonce`. The receiver (PC) accepts such an ERROR only while it is waiting for the answer to that HELLO; one that arrives after a session has started is ignored. Every other ERROR carries the session's `session_id`.

</details>

Main reasons (other texts can also arrive; display the received text as it is):

| Text | Meaning |
|---|---|
| `BUSY` | iOS is already streaming v3 to another receiver, is sending with v1/v2 or Bluetooth, is transferring a recording (in iFacialMocap and iFacialMocapTr, only a transfer over Bluetooth; 3.8.1), or is exporting an FBX file. In iFacialMocap and iFacialMocapTr, this is also the reason while a destination is set on the iOS side (a manual start or a v1/v2 destination). Exception: v1/v2 streaming that a PC handshake started does not give `BUSY`; it is replaced (in Facemotion3d, only streaming to the software name "Other"; 3.8.1) |
| `NOT_ALLOWED` | Purchase state or trial time does not allow streaming now |
| `PC_CONNECTION_DISABLED` | "Settings → Other functions → No connection accepted from PC" is on in Facemotion3d (the answer to a UDP HELLO). While this setting is on, iOS does not listen on TCP 49984, so over TCP the connection itself fails. A manual start from iOS still works |
| `V3_NOT_AVAILABLE`, `APP_INACTIVE` | The app is not showing its tracking screen, is in the background, or cannot use the camera. In iFacialMocapTr this is also the reason when trial conditions do not allow streaming now |
| `CAMERA_UNAVAILABLE` | The device does not support face tracking, or camera access is not allowed |
| `SCHEMA_NOT_READY` | The app could not prepare the first table within 5 s. A face that has not been found yet is not a reason ([3.6](#frame)) |
| `UNSUPPORTED_CONTRACT: requires 6` | The HELLO had another revision. iOS never switches to the revision-5 format (JSON) |
| `UNSUPPORTED_MODE` | The requested mode is unknown or not supported. iOS never switches to another mode on its own |
| `INVALID_HELLO`, `HELLO_SETTINGS_CHANGED` | HELLO values out of range, or the same nonce was sent with different settings |
| `SCHEMA_ACK_TIMEOUT` | UDP: no valid SCHEMA_ACK arrived within 10 s, so iOS ended the session |

#### 3.8.1 Relation to v1/v2 streaming

In every app, a new PC handshake (the request a v1/v2 receiver program sends to iOS to ask it to start sending) wins over v1/v2 streaming that an earlier PC handshake started. A v3 HELLO from a receiver (PC) is therefore not refused because of such streaming (in Facemotion3d, only streaming to the software name "Other"; streaming to any other software name gives `BUSY`).

<details>
<summary>Details: what is replaced, what is not, and the opposite case (per app)</summary>

**iFacialMocap, iFacialMocapTr**

- **Replaced by the v3 HELLO**: v1/v2 sending to the address that iOS learned from a PC's UDP handshake (with the iOS destination setting "default", this includes the TCP connection that iOS opened to the PC after such a handshake), and a v1/v2 TCP connection that a PC opened to iOS. iOS stops this sending, closes these connections and forgets the learned address, then starts the v3 session. The v1/v2 receiver gets no message; its data just stops. The v1/v2 sending does not resume when the v3 session ends.
- **Not replaced, so `BUSY`**: a destination that the user set on the iOS side (a fixed IP address, an address entered with the Live button, or the destination of a v3 start from iOS), a Bluetooth connection (including a recording transfer over Bluetooth), exporting an FBX file, and a v3 session that is already running (with another receiver, or started from iOS).
- **Not affected**: a recording transfer over TCP (a PC pulling recorded frames from port 49984, or iOS connecting to the PC's port 49987 to send a recording that the PC asked for). It does not make the HELLO `BUSY`, and the v3 HELLO does not end it.

**Facemotion3d**

- **Replaced by the v3 HELLO**: only v1/v2 streaming to the software name "Other" that a PC handshake (over UDP or TCP) started. iOS stops this streaming, closes the TCP connection that iOS opened to the PC and forgets the learned address, then starts the v3 session. The v1/v2 receiver gets no message; its data just stops. The v1/v2 streaming does not resume when the v3 session ends.
- **Not replaced, so `BUSY`**: streaming to any other software name, streaming started with the Live button, streaming started over Bluetooth, exporting or transferring a recording (over TCP or Bluetooth), exporting an FBX file, and a v3 session that is already running.

**The opposite case (iFacialMocap and iFacialMocapTr only).** While v3 is not selected as the sending protocol in the iOS app's settings (or the app's own v3 connection to its saved destination has failed), a new v1/v2 handshake from a PC ends a running v3 session that a PC started. The v3 receiver (PC) gets no message; the stream stops as described in "When iOS stops sending" ([section 2](#udp)). In Facemotion3d, a v1/v2 handshake that arrives during a v3 session is not accepted, and the v3 session continues.

</details>

<a id="fragments"></a>
### 3.9 UDP fragments

Splitting happens mainly to variable-mode SCHEMA. Fixed52 SCHEMA and FRAME are never split, so a receiver (PC) that accepts only fixed52 can skip reassembly and drop any message whose `part_count` is not 1 as invalid.

SCHEMA and FRAME may be larger than one datagram. The body is then cut into equal pieces, and the last fragment holds the rest. SCHEMA's start information (20 bytes) and descriptor (6 bytes) appear only once, at the start of the joined body.

| Message | Largest datagram | Body per fragment (`capacity`) |
|---|---|---|
| SCHEMA | 576 bytes, always | 536 |
| FRAME | `max_udp_size` from the start information | `max_udp_size − 40` |

`part_count = max(1, ceil(total_payload_length / capacity))`. All fragments share every header field except `part_index` and `chunk_length`. Fragments can arrive in any order. Identical duplicates are fine; a fragment that contradicts the others makes that message invalid. Other messages are never split.

<details>
<summary>Details: how much splitting to expect, and why SCHEMA is fixed at 576 bytes</summary>

| Message | Example | Splitting |
|---|---|---|
| SCHEMA (variable) | the standard 52 names (strings returned by ARKit) with app/profile `Facemotion3d` / `facemotion3d-other` (body 716 bytes) | 2 fragments (`part_count = 2`) |
| SCHEMA (fixed52) | body 26 bytes, total 66 bytes | none (`part_count = 1`) |
| FRAME (fixed52) | body 256 bytes, total 296 bytes | none (`part_count = 1`), even at the 576-byte minimum |
| FRAME (variable) | 52 names as i16 (body 152 bytes, 192 bytes in total) | none (`part_count = 1`). With the 1200-byte limit, up to 556 i16 values fit without splitting |

A SCHEMA is sent before the datagram size has been agreed (the `max_udp_size` in its start information), and a manual start has no HELLO at all. It is therefore fixed at 576 bytes, a size every receiver (PC) can take. FRAME uses the `max_udp_size` agreed in the SCHEMA.

</details>

<a id="changes"></a>
## 4. Table changes

Only in **variable mode** does the table change during a stream (in fixed52 it never changes; [1.4](#modes)). The last part of this section, "Sending SCHEMA again", applies to both modes.

A new `schema_id` means the numbers are ordered differently: BlendShapes were added, removed, renamed or reordered, or the value type changed from i16 to i32. A name counts as renamed whenever its code points (its UTF-8 bytes) change, even when it looks the same ([3.4.3](#variable-names)). The `sequence` counter continues.

For every FRAME it sends, iOS compares the order of BlendShape names and the value type with the current table, and sends a new table when they differ. Within one stream, the table changes mainly in these cases:

- Playback of a recording starts, and the recording's order of BlendShape names or its value type differs from the current table. With the same list and type the table stays the same, and there is no SCHEMA and no SCHEMA_ACK (whether values come from playback is shown by the FRAME `flags`, [3.2](#header)).
- A BlendShape value no longer fits the i16 range (−32768 to 32767). The value type becomes i32 and does not go back to i16 until the stream ends. Usual values are about 0 to 100, so this is rare.

<details>
<summary>Details: per-app differences in playback, and voice input</summary>

- In every app, the names are sent in UTF-8 byte (alphabetical) order, both live and during playback. The value type is decided from the whole recording (i32 if any value does not fit i16).
- iFacialMocap and iFacialMocapTr: the names in the recording are sent, so a recording with the live BlendShapes keeps the table.
- Facemotion3d: during playback it keeps sending the same name list as live (the standard 52, its own names such as `FM_SAD`, and the voice-input names). The order of names changes only when the recording contains a BlendShape name that is not in the current list (names missing from the recording are sent with the value 0).
- In every app, a playback that reaches the end of the recording sends the last frame before playback ends (when the user stops playback, it stops at once).
- When playback ends and live values return, the table changes again if the order of names differs from the live one.

**An example where the table does not change (Facemotion3d voice input).** The BlendShapes registered in the voice-input settings are in the list from the start of the stream, and are sent with the value 0 while they are not reacting. Speaking therefore never makes the list longer or shorter. Facemotion3d's BlendShape settings (the base list, the BlendShapes registered for voice input, name replacement) are fixed when the stream starts; changes apply from the next stream.

</details>

**How a new table arrives.**

- **UDP**: iOS stops FRAMEs, sends the new SCHEMA, waits for its SCHEMA_ACK, then continues with the newest values.
- **TCP**: iOS writes the new SCHEMA before the first FRAME that uses it.

The receiver (PC) switches to the new table only after the new SCHEMA is complete and valid. Until then it may keep reading FRAMEs of the old `schema_id` with the old table.

**Sending SCHEMA again (both modes).** A given `session_id` and `schema_id` always mean exactly the same bytes; a SCHEMA sent again that differs is invalid. When iOS receives GET_SCHEMA or the same HELLO again, it sends the current SCHEMA again (over UDP at most once per second). Over UDP it also sends it again every second while no SCHEMA_ACK has arrived. After SCHEMA_ACK has arrived, it does not send it again unless asked. When a table that the receiver (PC) has already stored arrives, the receiver (PC) replies with SCHEMA_ACK again (UDP, at most once per second).

<a id="timers"></a>
## 5. Timers and recovery

All times are defaults in seconds. "Valid" means a message that passed every check in [section 7](#limits).

| Who | Waits for | Limit | Then |
|---|---|---|---|
| Receiver (PC) | SCHEMA after HELLO (UDP) | 5 HELLOs, 1 s apart | Stop sending; keep waiting (WAITING) |
| Receiver (PC) | TCP connection to iOS | 3 s per attempt, 5 attempts, 1 s apart | Keep waiting (WAITING) |
| Receiver (PC) | complete first SCHEMA on a new TCP connection | 5 s after the connection is made | Close that connection only |
| Receiver (PC) | a new valid FRAME | 3 s | Recovery: up to 3 times, 1 s apart |
| Receiver (PC) | a new valid FRAME | 12 s | WAITING |
| Receiver (PC) | — | every 3 s | PING (not while WAITING) |
| iOS | SCHEMA_ACK (UDP) | send SCHEMA again every 1 s, give up after 10 s | ERROR `SCHEMA_ACK_TIMEOUT`, session ends |
| iOS | a valid message from the receiver (PC) | `lease_ms` (10 s) | Session ends |
| iOS | complete HELLO on a new TCP connection | 5 s after the connection is made (on iFacialMocap/iFacialMocapTr port 49984, 60 s for a connection on which nothing arrives; [section 6](#tcp)) | Closes the connection |

- **Recovery** = send SCHEMA_ACK for the stored table again (UDP only) and send GET_SCHEMA with `schema_id = 0`.
- The FRAME timer starts when the first SCHEMA is stored and restarts with every new valid FRAME. SCHEMA and PONG do **not** restart it. A frame with the face lost still counts.
- **WAITING** is not an exit. Keep the port open and keep reading. Send nothing on a timer (no HELLO, PING, GET_SCHEMA or reconnect). Answer a valid SCHEMA with SCHEMA_ACK. A valid FRAME returns to streaming. A manual start from iOS can begin a new session.
- While a session is streaming, a SCHEMA from another session is ignored: nobody can take over a running stream.

<details>
<summary>Details: which SCHEMA_ACK counts for lease_ms, and what the reference receiver does</summary>

- For the iOS `lease_ms`, a SCHEMA_ACK counts only when it acknowledges the current SCHEMA that iOS has already sent. An ACK for another `schema_id` is ignored and does not extend `lease_ms`.
- The sample also enters WAITING when iOS refuses the HELLO with ERROR, when nothing valid arrives for `lease_ms`, or when iOS sends ERROR for the session.

Receiver states used by the sample: `WAIT_SCHEMA` → `WAIT_FRAME` → `STREAMING` ⇄ `RECOVERING` → `WAITING`.

</details>

<a id="tcp"></a>
## 6. TCP (alternative)

TCP carries the same messages, with these differences:

- **Normal start**: connect to the iOS TCP port 49984, send HELLO on that connection right after connecting, and receive SCHEMA, then FRAMEs. **No SCHEMA_ACK.** If you send HELLO right after connecting, the time limits in the table below never close the connection.
- **Manual start**: iOS connects to the receiver (PC) TCP port **49984** and sends SCHEMA, then FRAMEs, on that connection. Keep this listener open while a normal-start connection is running. A connection from iOS that arrives while a normal start is waiting for the answer to its HELLO is also accepted for a manual start ("Manual start" in [section 2](#udp)). Over TCP the receiver (PC) connects again instead of resending HELLO ([section 5](#timers)), so after accepting that manual start (its SCHEMA), stop connecting again and close the connection that you opened for the normal start.
- **Message boundaries**: read 40 bytes, check the header, then read `chunk_length` bytes. Repeat. A read can contain part of a message or several messages. TCP messages are never split (`part_count = 1`), and there is no extra length prefix.
- **One connection, one session**: a session exists only on the connection that delivered its SCHEMA. A new connection starts with its own SCHEMA, even from the same address and port.
- **Errors**: when the stream is malformed, close that connection and keep listening. Do not search for the next `FMV3`.
- Recovery on a connection that stays open uses GET_SCHEMA only. PING is also sent every 3 s (for the reasons in [section 2](#udp)).

<details>
<summary>Details: how the reference receiver handles TCP connections (manual start)</summary>

The reference receiver works as follows:

- It does not close the normal-start connection, or the connection of a session in WAITING, just because iOS connected.
- When a valid SCHEMA arrives on the new connection, it starts the session on that connection and closes every other connection.
- When no complete SCHEMA arrives on the new connection within 5 s of accepting it, or invalid data arrives on it, it closes only that connection. A normal start that is waiting for the answer to its HELLO goes on (including its connection retries), and a session in WAITING stays as it is.
- If iOS connects again before a SCHEMA has arrived, it closes the earlier connection and keeps the newer one.

</details>

<details>
<summary>Details: when iOS closes a connection to TCP 49984 (per app)</summary>

iFacialMocap and iFacialMocapTr also accept the v1/v2 TCP commands on this port, and tell v3 and v1/v2 apart by the first bytes that arrive.

| What arrives after connecting | iFacialMocap, iFacialMocapTr | Facemotion3d |
|---|---|---|
| Data starting with `FMV3` (including only the beginning, such as `F`) | Closed if no complete HELLO arrives within 5 s of connecting | Closed if no complete HELLO arrives within 5 s of connecting |
| Nothing | Closed 60 s after connecting (to wait for the first v1/v2 command) | Closed 5 s after connecting |
| A v1/v2 command | Handled as a v1/v2 connection; this time limit does not apply | Only the iFacialMocap-compatible recording-transfer commands are accepted (closed if the command is not complete within 2 s of connecting) |

</details>

<a id="limits"></a>
## 7. Limits and validation

Check each message before using it. For UDP, drop an invalid datagram. For TCP, close the connection.

The main checks are the form of the header (`magic`, `header_size`, the type number, `session_id`), the body length fixed for each message, the SCHEMA contents (the rules of [3.4](#schema)), whether the message belongs to the current session, and that all 12 floating-point numbers of a FRAME are finite.

<details>
<summary>Details: the full list of checks</summary>

| Item | Rule |
|---|---|
| Header (all types) | `magic` is `FMV3`, `header_size` 40, type 1–9, nonzero `session_id`. A datagram (over TCP, a message) is 40 + `chunk_length` bytes |
| Header (per type) | Every field that the "Values per type" table of [3.2](#header) marks "0 (unused)" is 0 (only the `sequence` of an ERROR may be ignored). `schema_id` is nonzero on SCHEMA, SCHEMA_ACK and FRAME. `flags` is used only on FRAME, and only bits 0 and 1 may be set. `source_token` is 0 except on FRAME |
| HELLO body | Exactly 10 bytes; revision 6; `requested_mode` 0 or 1 |
| SCHEMA body | Variable: 30–262144 bytes, parsed exactly to the end of the body. Fixed52: exactly 26 bytes; descriptor `01 00 02 01 34 00` ([3.4.2](#descriptor)), `schema_id = 1`, `part_count = 1` |
| SCHEMA contents | Known mode and value type, `schema_format_version = 1`, valid lengths and UTF-8, the count and duplicate rules of [3.4.3](#variable-names), no U+0000–U+001F in names, no extra bytes at the end, no mode change within a session |
| SCHEMA start information | `contract_revision` 6, `actual_fps` 1–60, `max_udp_size` 576–1200, `lease_ms` 5000–60000. These do not change within a session |
| FRAME body | Variable: exactly (`N × 2` or `N × 4`) + 48 bytes, at most 16432. Fixed52: exactly 256 bytes, with `schema_id = 1` and `part_count = 1`. In both modes, all 12 floating-point numbers are finite |
| ERROR body | 1–512 bytes, valid UTF-8 |
| SCHEMA_ACK, GET_SCHEMA, PING, PONG, STOP body | 0 bytes |
| Fragments | Up to 512 per message. Keep at most 8 incomplete messages and 524288 bytes. Drop an incomplete FRAME after 0.25 s and an incomplete SCHEMA after 3 s |
| New session (normal start) | `client_nonce` and mode equal your HELLO, and `actual_fps` and `max_udp_size` are not above your request |
| New session (manual start) | `client_nonce = 0`. `actual_fps` and `max_udp_size` may be any value in the ranges above. iOS has not received any settings from the receiver (PC), so do not compare these values with your local settings. Also accept it when it arrives while a normal start is waiting for the answer to its HELLO |
| New session (both) | The receiver (PC) accepts that mode ("Manual start" in [section 2](#udp)). Only one incomplete new session at a time (3 s) |
| Old sessions | Ignore late messages of a session that was replaced (the sample remembers 32) |

</details>

A frame is never accepted without validation, even when rendering is late.

<a id="compat"></a>
## 8. Compatibility

**Revision 6** requires both the receiver (PC) and the iOS app to support revision 6. Revision 5 and revision 6 do not work together, and iOS never switches back to the revision-5 format (JSON). Retries do not help, so update both sides together. Revisions 4 and 5 were used only by beta builds before the first App Store release of v3. The changes in each revision, and what happens when revision 5 and revision 6 are mixed, are in the [CHANGELOG](CHANGELOG.md#english). For the supported app versions, see section 5 of the [README](README.md).

The answer key `golden_vectors.h` / `golden_vectors.c` (the expected bytes in C99, [Appendix A](#examples)) is optional material for development and is never sent. No JSON is ever sent over the network.

v3 is an additional protocol. The v1/v2 text protocols of the apps are unchanged, and a v1/v2 receiver program cannot read v3.

<a id="examples"></a>
## Appendix A Byte examples

All bytes are values from the answer key `golden_vectors.c` (each example first gives its name in that file). "Position" is the byte position from the start of the message. The position inside the body is that value minus 40.

<details>
<summary>Details: the answer key in C (golden_vectors.h, golden_vectors.c)</summary>

| Field | Content |
|---|---|
| `body` | The whole body |
| `client_nonce` … `contract_revision` | The start information (the first 20 bytes of the SCHEMA body) |
| `table.layout` | The SCHEMA body from the descriptor on. In a FRAME vector, the table to read that FRAME with |
| `udp`, `udp_count` | The datagrams sent over UDP, in order, each with its length |
| `message` | The whole message in one piece (as sent over TCP; SCHEMA_ACK is sent only over UDP) |
| `table.names`, `values`, `pose_bits` | Names as UTF-8 bytes with a length, values, and the head and eye values as Float32 bit patterns |

`golden_vectors_check.c` decodes every vector with little-endian reads and compares it with these fields, then encodes it again and compares the bytes with `memcmp`. It needs only a C99 compiler, no network and no other library: `cc -std=c99 -o golden_vectors_check golden_vectors.c golden_vectors_check.c && ./golden_vectors_check`.

</details>

### A.1 HELLO (normal start, requesting fixed52, 50 bytes)

<details>
<summary>Open the bytes</summary>

`hello_fixed52`:

| Position | Bytes | Meaning |
|---:|---|---|
| 0 | `46 4d 56 33 01 00 28 00` | `magic` (ASCII `FMV3`), type 1 = HELLO, `flags` 0, `header_size` 40 |
| 8 | `11 22 33 44 55 66 77 88` | `session_id` field: the `client_nonce` picked by the receiver (PC) |
| 16 | 12 × `00` | `schema_id`, `sequence`, `source_token`: all 0 |
| 28 | `0a 00 00 00 00 00 01 00 0a 00 00 00` | `total_payload_length` 10, `part_index` 0, `part_count` 1, `chunk_length` 10 |
| 40 | `3c 00 40 02` | Body: `requested_fps` 60, `max_udp_size` 576 |
| 44 | `06 00 00 00 01 00` | Body: `contract_revision` 6, `requested_mode` 1 (fixed52) |

</details>

### A.2 Fixed52 manual start (UDP)

<details>
<summary>Open the bytes (SCHEMA, SCHEMA_ACK, FRAME)</summary>

`fixed52_manual`, `schema_ack`, `fixed52_i32`: an example from the moment iOS starts sending, going through SCHEMA, SCHEMA_ACK and FRAME. The `session_id` is `08 07 06 05 04 03 02 01`, picked by iOS (the UInt64 0x0102030405060708).

**SCHEMA (66 bytes)**

| Position | Bytes | Meaning |
|---:|---|---|
| 0 | `46 4d 56 33 02 00 28 00` | `magic`, type 2 = SCHEMA, `flags` 0, `header_size` 40 |
| 8 | `08 07 06 05 04 03 02 01` | `session_id` |
| 16 | `01 00 00 00`, 8 × `00` | `schema_id` 1; `sequence` and `source_token` are 0 |
| 28 | `1a 00 00 00 00 00 01 00 1a 00 00 00` | Body of 26 bytes, not split (`part_count` 1) |
| 40 | `00 00 00 00 00 00 00 00` | Start information, `client_nonce`: 0 (a manual start has no HELLO) |
| 48 | `3c 00 40 02 10 27 00 00 06 00 00 00` | `actual_fps` 60, `max_udp_size` 576, `lease_ms` 10000 (10 s), `contract_revision` 6 |
| 60 | `01 00 02 01 34 00` | Descriptor: `mode` 1 (fixed52), `blend_encoding` 2 (i32), `schema_format_version` 1, `blend_count` 52 |

In a normal start, the 8 bytes from position 40 hold the `client_nonce` sent in HELLO (for A.1, `11 22 33 44 55 66 77 88`).

**SCHEMA_ACK (40 bytes)**: `46 4d 56 33 03 00 28 00 08 07 06 05 04 03 02 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00`. Type 3, the same `session_id` as the SCHEMA and the stored `schema_id` 1; everything else is 0 except `part_count` (`01 00` at position 34).

**FRAME (296 bytes)**

| Position | Bytes | Meaning |
|---:|---|---|
| 0 | `46 4d 56 33 04 03 28 00` | `magic`, type 4 = FRAME, `flags` 3 (playback; the camera tracks the face now), `header_size` 40 |
| 8 | `08 07 06 05 04 03 02 01` | `session_id` |
| 16 | `01 00 00 00 07 00 00 00 78 56 34 12` | `schema_id` 1, `sequence` 7, `source_token` (the receiver (PC) may ignore it) |
| 28 | `00 01 00 00 00 00 01 00 00 01 00 00` | Body of 256 bytes, not split (`part_count` 1) |
| 40 | `e7 ff ff ff` | Index 0 `browDownLeft`: −25 (−0.25) |
| 136 | `96 00 00 00` | Index 24 `jawOpen`: 150 (1.5) |
| 244 | `70 11 01 00` | Index 51 `tongueOut`: 70000 (does not fit i16, but fixed52 is always i32, so it is sent as it is) |
| 248 | `00 00 a0 3f` `00 00 20 c0` `00 00 70 40` | Head rotation rx, ry, rz: 1.25, −2.5, 3.75 (degrees) |
| 260 | `cd cc cc 3d` `cd cc 4c be` `9a 99 99 3e` | Head position px, py, pz: 0.1, −0.2, 0.3 (meters) |
| 272 | `00 00 80 40` `00 00 a0 40` `00 00 c0 40` | Right eye rotation rx, ry, rz: 4, 5, 6 (degrees) |
| 284 | `00 00 e0 40` `00 00 00 41` `00 00 10 41` | Left eye rotation rx, ry, rz: 7, 8, 9 (degrees) |

Positions 44–135 (indices 1–23) and 140–243 (indices 25–50) are all `00` (value 0).

</details>

### A.3 Variable-mode SCHEMA and FRAME

<details>
<summary>Open the bytes (SCHEMA, FRAME)</summary>

`initial_facemotion3d`: the answer to a HELLO that requested variable mode (`hello`; its `client_nonce` is `11 22 33 44 55 66 77 88`, and its body is the variable example in [3.3](#hello)). iOS chooses 30 fps and 700 bytes and sends 3 names. With so few names the SCHEMA is not split: it is one datagram of 138 bytes in total. The header (positions 0–39) is `46 4d 56 33 02 00 28 00 08 07 06 05 04 03 02 01 01 00 00 00`, 8 × `00`, `62 00 00 00 00 00 01 00 62 00 00 00` (body of 98 bytes, not split).

| Position | Bytes | Meaning |
|---:|---|---|
| 40 | `11 22 33 44 55 66 77 88` | `client_nonce`: the value sent in HELLO |
| 48 | `1e 00 bc 02 10 27 00 00 06 00 00 00` | `actual_fps` 30, `max_udp_size` 700, `lease_ms` 10000, `contract_revision` 6 |
| 60 | `00 00 01 01 03 00` | Descriptor: `mode` 0 (variable), `blend_encoding` 1 (i16), `schema_format_version` 1, `blend_count` 3 |
| 66 | `0c 00` `46 61 63 65 6d 6f 74 69 6f 6e 33 64` | `app`: length 12, `Facemotion3d` |
| 80 | `12 00` `66 61 63 65 6d 6f 74 69 6f 6e 33 64 2d 6f 74 68 65 72` | `profile`: length 18, `facemotion3d-other` |
| 100 | `07 6a 61 77 4f 70 65 6e` | Name 1: length 7, `jawOpen` |
| 108 | `0f e7 8b ac e8 87 aa e3 81 ae e7 ac 91 e9 a1 94` | Name 2: length 15 (5 characters, but 15 bytes), `独自の笑顔` (Japanese for "custom smile") |
| 124 | `0d 6d 79 43 75 73 74 6f 6d 53 6d 69 6c 65` | Name 3: length 13, `myCustomSmile` |

Next is a FRAME read with this table (3 names, i16) (`i16_manual`, 94 bytes in total). In `golden_vectors.c` it is paired with a table of the names `a`, `b` and `c`, but a FRAME carries no names, so the bytes are the same for every table with 3 names and i16.

| Position | Bytes | Meaning |
|---:|---|---|
| 0 | `46 4d 56 33 04 01 28 00 08 07 06 05 04 03 02 01` | Type 4 = FRAME, `flags` 1 (live; the face is tracked), `session_id` |
| 16 | `01 00 00 00 09 00 00 00 44 33 22 11` | `schema_id` 1, `sequence` 9, `source_token` |
| 28 | `36 00 00 00 00 00 01 00 36 00 00 00` | Body of 54 bytes (2 × 3 + 48), not split |
| 40 | `e7 ff` `64 00` `00 00` | Values 1 to 3 (i16): −25, 100, 0. With the SCHEMA above: `jawOpen` −25, `独自の笑顔` 100, `myCustomSmile` 0 |
| 46 | 48 × `00` | The 12 values of the head, right eye and left eye: all 0.0 |

</details>

## Appendix B Files in this repository

| File | Use |
|---|---|
| `face_motion_v3.py` | Reference receiver (UDP and TCP) and encoder/decoder |
| `simulate_ios_v3.py` | Simulated iOS sender for tests without a phone; not the real app |
| `golden_vectors.h`, `golden_vectors.c`, `golden_vectors_check.c` | The answer key in C99: expected bytes for HELLO, SCHEMA_ACK, control messages, SCHEMA and FRAME (UDP and TCP), and a program that checks them ([Appendix A](#examples)). Optional, for development; never sent |
| `diagrams/fmv3_flow.svg`, `diagrams/fmv3_messages.svg` | Overview pictures of the connection flow and the byte layout; this document is the definition |

Names in the Python sample: `Packet.kind` = `message_type`, `Packet.total_length` = `total_payload_length`, `len(Packet.payload)` = `chunk_length`. What the sample returns in fixed52 mode (the mode name, empty `app` and `profile`, the standard 52 names) is in section 6 of the [README](README.md).

<a id="fixed52-names"></a>
## Appendix C Fixed52 names and indices

<details>
<summary>Open the table of the standard 52 (index, ARKit identifier, string returned by ARKit, position in FRAME)</summary>

The following 52 values (the standard 52) are always sent as **Int32**, in this order. The order is the alphabetical order of the "Name" column (the ARKit identifiers). Indices count from 0, and byte offsets are from the beginning of the FRAME body (add 40 for the whole message).

| Index | Name (ARKit identifier) | String returned by ARKit | FRAME body offset |
|---:|---|---|---:|
| 0 | `browDownLeft` | `browDown_L` | 0 |
| 1 | `browDownRight` | `browDown_R` | 4 |
| 2 | `browInnerUp` | (same) | 8 |
| 3 | `browOuterUpLeft` | `browOuterUp_L` | 12 |
| 4 | `browOuterUpRight` | `browOuterUp_R` | 16 |
| 5 | `cheekPuff` | (same) | 20 |
| 6 | `cheekSquintLeft` | `cheekSquint_L` | 24 |
| 7 | `cheekSquintRight` | `cheekSquint_R` | 28 |
| 8 | `eyeBlinkLeft` | `eyeBlink_L` | 32 |
| 9 | `eyeBlinkRight` | `eyeBlink_R` | 36 |
| 10 | `eyeLookDownLeft` | `eyeLookDown_L` | 40 |
| 11 | `eyeLookDownRight` | `eyeLookDown_R` | 44 |
| 12 | `eyeLookInLeft` | `eyeLookIn_L` | 48 |
| 13 | `eyeLookInRight` | `eyeLookIn_R` | 52 |
| 14 | `eyeLookOutLeft` | `eyeLookOut_L` | 56 |
| 15 | `eyeLookOutRight` | `eyeLookOut_R` | 60 |
| 16 | `eyeLookUpLeft` | `eyeLookUp_L` | 64 |
| 17 | `eyeLookUpRight` | `eyeLookUp_R` | 68 |
| 18 | `eyeSquintLeft` | `eyeSquint_L` | 72 |
| 19 | `eyeSquintRight` | `eyeSquint_R` | 76 |
| 20 | `eyeWideLeft` | `eyeWide_L` | 80 |
| 21 | `eyeWideRight` | `eyeWide_R` | 84 |
| 22 | `jawForward` | (same) | 88 |
| 23 | `jawLeft` | (same) | 92 |
| 24 | `jawOpen` | (same) | 96 |
| 25 | `jawRight` | (same) | 100 |
| 26 | `mouthClose` | (same) | 104 |
| 27 | `mouthDimpleLeft` | `mouthDimple_L` | 108 |
| 28 | `mouthDimpleRight` | `mouthDimple_R` | 112 |
| 29 | `mouthFrownLeft` | `mouthFrown_L` | 116 |
| 30 | `mouthFrownRight` | `mouthFrown_R` | 120 |
| 31 | `mouthFunnel` | (same) | 124 |
| 32 | `mouthLeft` | (same) | 128 |
| 33 | `mouthLowerDownLeft` | `mouthLowerDown_L` | 132 |
| 34 | `mouthLowerDownRight` | `mouthLowerDown_R` | 136 |
| 35 | `mouthPressLeft` | `mouthPress_L` | 140 |
| 36 | `mouthPressRight` | `mouthPress_R` | 144 |
| 37 | `mouthPucker` | (same) | 148 |
| 38 | `mouthRight` | (same) | 152 |
| 39 | `mouthRollLower` | (same) | 156 |
| 40 | `mouthRollUpper` | (same) | 160 |
| 41 | `mouthShrugLower` | (same) | 164 |
| 42 | `mouthShrugUpper` | (same) | 168 |
| 43 | `mouthSmileLeft` | `mouthSmile_L` | 172 |
| 44 | `mouthSmileRight` | `mouthSmile_R` | 176 |
| 45 | `mouthStretchLeft` | `mouthStretch_L` | 180 |
| 46 | `mouthStretchRight` | `mouthStretch_R` | 184 |
| 47 | `mouthUpperUpLeft` | `mouthUpperUp_L` | 188 |
| 48 | `mouthUpperUpRight` | `mouthUpperUp_R` | 192 |
| 49 | `noseSneerLeft` | `noseSneer_L` | 196 |
| 50 | `noseSneerRight` | `noseSneer_R` | 200 |
| 51 | `tongueOut` | (same) | 204 |

</details>
