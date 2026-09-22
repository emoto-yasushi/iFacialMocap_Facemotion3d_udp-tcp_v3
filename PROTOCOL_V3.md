# Face Motion v3 Common Wire Protocol — Standard Ports (`contract_revision = 4`)

Source document date: 2026-09-18. Applies to iFacialMocap / iFacialMocapTr / Facemotion3d. This document specifies the wire protocol.
The data transmitted and received must follow this specification. `face_motion_v3.py` is the reference receiver; `simulate_ios_v3.py` is a synthetic sender. The small distribution does not include the iOS Codex instructions or the maintainer's test suite.
The iOS implementation must follow this specification. `contract_revision = 4` identifies the wire format; it is not the number of Codex instructions issued or the number of times the iOS code has been implemented.

> **English edition — documentation corrections dated 2026-09-22:** This edition and the [Japanese specification](PROTOCOL_V3_JA.md) have been updated together following the recheck. The corrections clarify the pending-HELLO ERROR exception, the scope of the UDP schema-send rate limit, and initial-schema validation on a new TCP connection. Header/payload layouts, field names, numeric constants, ports, and `contract_revision = 4` are unchanged. Appendix A records known differences in the reviewed code snapshot; it does not authorize those differences or mean that the code has been fixed.

## Supported apps and minimum versions

| App | Supported version |
|---|---|
| **iFacialMocap** | **1.5.3 or later** |
| **iFacialMocapTr** | **1.2.6 or later** |
| **Facemotion3d** | **1.4.6 or later** |

**This sample supports only the versions listed above and later versions of each app. Earlier versions are not supported.** Update the app before running the sample if it is below the listed minimum version.

## Read first — do not exit the PC receiver when retries run out

This specification provides **FRAME-specific monitoring + bounded recovery attempts + indefinite receive-only waiting + manual sending from iOS**.
Normal UDP startup requires acknowledgement that the schema has been received.

```text
Store schema / send ACK -> wait for numeric data -> no FRAME for 3 s -> up to 3 recovery attempts
                                                                            |
                                                                  still no FRAME
                                                                            v
                                                    enter WAITING by the 12 s deadline
                                                    keep the receiving socket/port open
                                                                            |
                                                        iOS starts sending manually later
                                                                            v
                                     receive SCHEMA -> validate/store -> reply with ACK -> receive FRAME
```

Receiving only PONGs or schemas does not extend the FRAME-specific deadline.
While waiting, the PC stops periodic requests, reconnection attempts, and PINGs, but replies with an ACK to a valid schema that arrives.
Termination is explicit: Ctrl+C, `stop_event`, a specified `duration`, a fatal local error, or equivalent. Lack of incoming data alone does not terminate the receiver.

## Normal UDP startup — wait for acknowledgement of the schema

```text
PC                                              iOS
 |                                               |
 |---- HELLO ----------------------------------->| (1) Request v3
 |                                               |
 |<--- SCHEMA [id 7] ----------------------------| (2) Start information + field order
 |     Collect all fragments; validate/store     |     Do not send numeric data yet
 |                                               |
 |---- SCHEMA_ACK [id 7] ----------------------->| (3) Schema 7 has been stored
 |                                               |     Check session / sender / schema ID
 |<--- FRAME [id 7] -----------------------------| (4) Start sending numeric data
 |<--- FRAME [id 7] -----------------------------|     Repeat from here
```

**Step (3) is neither a second start request nor a separate “start accepted” response. It acknowledges that step (2) reached the PC.**
SCHEMA also serves as the response to the start request. Do not introduce a dedicated positive-start response or a separate permission-to-start packet.

**On UDP, both at startup and after a schema change, iOS must not send FRAME until it has received SCHEMA_ACK for the current schema.**
If the acknowledgement does not arrive, retransmit the schema. The exchange must recover from a lost schema as well as a lost ACK.
TCP is an ordered stream: `HELLO -> SCHEMA -> FRAME`. An application-layer SCHEMA_ACK is not required.
If the PC cannot store the schema, it must not interpret subsequent numeric data; disconnect with an error.

There is no application-layer acknowledgement or retransmission for each individual numeric FRAME. See **5.4** for schema changes and **7.1–7.4** for retransmission.
See `README.md` for startup commands. The reference sender-side ACK/retry logic is the `SchemaDelivery` class inside `simulate_ios_v3.py`.

## 1. Purpose, compatibility, and connections

Use a **changeable schema**, an **order fixed for the lifetime of that schema**, and **binary values for every field**. Do not hard-code a count of 52.
Only the schema is UTF-8 JSON. FRAME must not contain names, JSON, CSV, compression, Base64, or text delimiters.
Do not omit zero values. Do not use delta encoding, references to a previous frame, or retransmission of old FRAMEs.

### Standard ports and connection directions

Do not create new, dedicated ports for v3. Add v3 dispatch to the existing app listeners.

| App | Transport | iOS listener for normal startup | PC receive listener, also used for manual startup/recovery |
|---|---|---:|---:|
| iFacialMocap | UDP | 49983 | 49983 |
| iFacialMocap | TCP | 49984 | 49986 |
| Facemotion3d | UDP | 49993 | 49983 |
| Facemotion3d | TCP | 49994 | 49986 |

- For UDP, the PC binds to the PC-side port above and sends HELLO. iOS replies from **the existing socket that received HELLO** to the PC's actual source IP address and port. The same PC socket receives all data.
- For normal TCP startup, the PC connects to the iOS port above. HELLO, SCHEMA, FRAME, and control messages all use **that one connection**. The PC may use an OS-assigned ephemeral source port for this connection. The PC's port 49986 listener remains open alongside it for manual startup and resumption from waiting; it does not mean opening another connection to port 49986 for replies to the normal connection.
- For manual TCP startup, iOS connects to PC:49986 and sends SCHEMA followed by FRAME without HELLO. It reads control messages on the same connection. The v3 TCP mode does not require UDP.
- Preserve iFacialMocap's **publicly documented legacy TCP mode** as a separate existing path: a legacy start string goes to UDP49983, then iOS connects to PC:49986 over TCP. PC-initiated v3 TCP in this specification shares the **direct TCP49984** listener used by `startListener()` in the original iOS code. Do not change the separate existing listener on 49985 or the recorded-data path on 49987.
- Facemotion3d uses 49993 for standard iOS UDP command reception. PC-side 49983 follows the Other output's default setting and the official Python example. However, the original code also has an automatic-connection branch that sends to PC49993. **Do not change that existing branch to 49983.** The v3 PC receiver can also be started with `--listen-port 49993`. Do not close or move the existing compatibility listener on iOS UDP49983.
- `--app ifacialmocap` (default) / `--app facemotion3d` and `--transport` select the Python defaults. `--port` overrides the iOS destination port; `--listen-port` overrides the PC listening port. This selects ports; it does not authenticate a sender.
- Manual sending uses the IP address and port selected by the user. Do not overwrite saved settings or defaults for existing formats. If existing user settings specify a different port, match the explicit settings at both ends.
- Do not close an already-bound PC socket/listener merely because data is not arriving. If another app owns the standard port, report a clear error. Do not silently switch ports or terminate another process.
- The PC and iOS normally run on different devices, so they can use the same UDP port number. Only when running both the simulator and receiver on one PC must you avoid a local bind conflict, for example by explicitly choosing a different sender port.

IPv4 is the default. Python uses one address family, IPv4 or IPv6, selected by `--bind`. Use `--bind ::` for IPv6. Validation on Windows, macOS, and real IPv6 devices is separately required.

**Set `contract_revision` to the constant 4 in both the end of the HELLO payload and the SCHEMA start information.**
Reject any `contract_revision` other than 4. Both ends must identify this specification, including manual startup with nonce=0 and indefinite receive waiting.
Message type 2 is reserved and must not be used. Receivers must not accept type=2 either.
Use only the message types listed in Section 3. Fragment a schema using SCHEMA and the common header; do not add separate start, continuation, or end message types.
The iOS implementation must agree with the reference Python, specification, and tests. Do not automatically fall back to a different `contract_revision`.

The scope is live data and live playback of recordings. Preserve the existing paths for bulk recording transfer, FBX, audio files, body data, and DCC-specific bridges.
Do not add, remove, or replace Bluetooth functionality. v3 supports one client at a time. Do not take over an existing transfer.

### Coexistence with existing formats

Use one existing receive endpoint and dispatch by the leading `FMV3` bytes. Pass non-v3 data to the existing handler. Do not reinterpret malformed v3 data as a legacy text command. Separating the v3 codec is not the same as adding listening ports.

## 2. Common header — the first 40 bytes of every message

All integers and Float32 values are **little-endian, with no padding**.
Python: `struct.Struct("<4sBBHQIIIIHHI")`. Do not send the native memory image of a Swift struct; write each value in order.

| Offset | Size | Type | Meaning |
|---:|---:|---|---|
| 0 | 4 | bytes | ASCII `FMV3` = `46 4d 56 33` |
| 4 | 1 | UInt8 | message_type |
| 5 | 1 | UInt8 | flags; 0 except on FRAME |
| 6 | 2 | UInt16 | header_size = 40 |
| 8 | 8 | UInt64 | Session identifier. Use client_nonce for HELLO and for an ERROR rejecting a pending HELLO; otherwise use the iOS-generated session_id. Must be nonzero |
| 16 | 4 | UInt32 | schema_id |
| 20 | 4 | UInt32 | sequence |
| 24 | 4 | UInt32 | source_token; used **only on FRAME**. 0 on every other message, including SCHEMA |
| 28 | 4 | UInt32 | total_payload_length; payload length before fragmentation, excluding the header |
| 32 | 2 | UInt16 | part_index; zero-based |
| 34 | 2 | UInt16 | part_count; 1 if unfragmented |
| 36 | 4 | UInt32 | chunk_length; number of payload bytes in this packet |
| 40 | variable | bytes | Payload, or a payload fragment |

FRAME flags: bit0 = currently tracking; bit1 = live playback of a recording. All other bits are 0.
SCHEMA, FRAME, and SCHEMA_ACK require a nonzero `schema_id`. GET_SCHEMA uses the requested ID or 0 for the latest schema. All other message types use 0.
`sequence` is 0 except on FRAME, PING, PONG, and ERROR. 0 is recommended for ERROR. A retransmitted SCHEMA always has sequence=0.

## 3. Message types

| Type (decimal) | Name | Direction | Payload |
|---:|---|---|---|
| 1 | HELLO | PC -> iOS | Start request, 8 bytes |
| 3 | SCHEMA | iOS -> PC | **20-byte start information + schema JSON** |
| 4 | FRAME | iOS -> PC | Integer array in schema order + 12 Float32 head/eye values |
| 5 | GET_SCHEMA | PC -> iOS | Empty; request retransmission of a schema |
| 6 | PING | PC -> iOS | Empty; keep the session alive |
| 7 | PONG | iOS -> PC | Empty; same sequence as the PING |
| 8 | STOP | PC -> iOS | Empty; end only the corresponding v3 session |
| 9 | ERROR | iOS -> PC | UTF-8 reason, 1–512 bytes |
| 10 | SCHEMA_ACK | PC -> iOS | Empty; identifies the validated, stored schema_id. **Required on UDP** |

## 4. What the PC sends first — HELLO

```text
[Common header 40 B] [requested_fps 2 B] [max_udp_size 2 B] [contract_revision 4 B]
                                                              Total: 48 B
```

Payload: `<HHI`. `requested_fps` is 1–60. `max_udp_size` is 576–1200, the maximum size of a FRAME datagram including its FMV3 header. `contract_revision` is 4.
In the header's `session_id` field, place a random, nonzero UInt64 `client_nonce` generated by the PC for that connection attempt sequence.
Set schema_id=sequence=source_token=flags=0, part_index=0, part_count=1, and payload length=8.

Example payload for 60 fps and a 1200-byte limit: `3c 00 | b0 04 | 04 00 00 00`.
The explanatory `|` separators are not transmitted. `hello_hex` in the supplied `golden_vectors.json` contains a complete 48-byte example.

iOS checks purchase status, usage conditions, and conflicts with other transfers before accepting. On failure, return only ERROR. The PC displays the reason and enters receive-only waiting without closing its receive endpoint.
On acceptance, **immediately return the following SCHEMA**. Do not send a separate positive acknowledgement.
UDP then waits for acknowledgement of that schema. TCP may continue with FRAME once SCHEMA has been queued first.
If building the schema requires the first AR update, send it as soon as that update is available; do not wait for another PC request.
If it cannot be prepared within 5 seconds, return a short ERROR and release the pending startup state.

A repeated HELLO from the same source/connection with the same nonce and contents returns the same `session_id` and the current SCHEMA.
On UDP, limit schema sends/retransmissions to at most once per second per peer. Do not reset frame numbers, schema numbers, or usage-time limits.
A repeated identical HELLO must not reset an existing ACK-wait deadline or an already-acknowledged state.
Reject changed request contents with the same nonce. Do not replace an in-use session with a different peer or nonce.

## 5. What iOS returns — SCHEMA = start information + schema

### 5.1 Overall layout

```text
[Common header 40 B] [Start information 20 B] [UTF-8 schema JSON, variable length]
                              |                              |
                    Establish session settings    Establish names and numeric type
```

The header's `session_id` is a random, nonzero UInt64 generated by iOS for each new stream.
`schema_id` is the current schema number, 1 or higher. Set source_token=flags=sequence=0.
**Every SCHEMA uses this same payload layout**, including the initial schema, changes, periodic retransmissions, and responses to retransmission requests.

### 5.2 Start information — the first 20 payload bytes

Python: `struct.Struct("<QHHII")`. JSON starts at payload offset=20, or offset=60 from the start of an unfragmented message.

| Payload offset | Size | Type | Meaning |
|---:|---:|---|---|
| 0 | 8 | UInt64 | client_nonce; echo the nonzero HELLO value for normal startup. **0 only for manual iOS startup** |
| 8 | 2 | UInt16 | actual_fps; 1 through requested_fps |
| 10 | 2 | UInt16 | max_udp_size; 576 through the requested limit, for FRAME UDP datagrams |
| 12 | 4 | UInt32 | lease_ms; default 10000, permitted range 5000–60000 |
| 16 | 4 | UInt32 | contract_revision = 4 |

The nonce, FPS limit, UDP limit, lease, and revision do not change within a session.
The configured actual output rate may be lower than `actual_fps`. If the upper bound itself needs renegotiation, use a new HELLO/session.
The PC must validate **all fragments, the start information, and the JSON before** adopting the session_id and schema together.
Do not adopt a session from partial fragments or a FRAME that arrives before the schema.
For normal startup, the nonzero nonce must match the value in the PC's HELLO for that startup. **0 is reserved for manual startup** and is accepted only by a PC that permits receive waiting for manual senders.
`V3Client` enables manual receive waiting. The lower-level `ReceiverCore` permits 0 only when `allow_push=True` is explicitly set.
A new session may be adopted only in WAIT_SCHEMA (startup) or WAITING (receive-only waiting).
Reject takeover by another session while streaming, waiting for the first FRAME, or performing recovery attempts.
With `--host`, accept input only from that host's IP addresses. UDP replies go to the source port of the received packet.
`--listen` without `--host` explicitly permits input from any sender on the trusted LAN.
Adopt a new session only after full schema validation and successful completion of `on_schema`. Discard the old schema, fragments, and sequence state.
Remember the 32 most recently replaced endpoint/session pairs to prevent rollback caused by old packets.
Allow only one incomplete candidate-session schema at a time; discard it after 3 seconds. nonce=0 is not authentication.

### 5.3 Schema JSON

Uncompressed UTF-8 JSON. The following nine keys are required. schema_version=1 is the version of the **JSON schema format**, separate from contract revision 4.

```json
{"schema_version":1,"app":"Facemotion3d","profile":"facemotion3d-other","blend_names":["eyeBlinkLeft","eyeBlinkRight","jawOpen","myCustomSmile"],"blend_encoding":"i16","blend_unit":"percent","pose_layout":"head_rxyz_pxyz_rightEye_rxyz_leftEye_rxyz","rotation_unit":"degree","position_unit":"meter"}
```

The app/profile pair is either `iFacialMocap` / `ifacialmocap-stream` or `Facemotion3d` / `facemotion3d-other`.
The order of `blend_names` is the order of transmitted values. Specify either i16 or i32 for all BlendShape values in that schema.

Names are case-sensitive, unique UTF-8 strings. Empty names are forbidden. Each name must be 1–255 bytes and must not contain U+0000–U+001F.
The field count is 0–4096. This is a defensive limit against invalid input, not a fixed list of types or a fixed count of 52.
The entire SCHEMA payload must be at most 262144 bytes; therefore, its JSON portion must be at most 262124 bytes.
Reject duplicate JSON keys, nonstandard constants such as NaN, unknown or missing keys, and invalid Unicode.

The sender builds and caches a stable order only when settings change. Do not enumerate a Dictionary to establish the order on every frame.
Include standard fields, FM_* fields, and configured custom audio BlendShapes; do not include only fields currently triggered by speech.
Keep inactive custom fields in the schema with values of 0. An intentional audio override of a standard name uses that same single field; when inactive, preserve the underlying value.
Diagnose unintended duplicate names introduced by remapping or similar operations. Do not transmit an ambiguous schema.

### 5.4 When the schema changes during streaming — pause UDP and acknowledge again

**Distinguish a numeric-value change from a schema change.**

| Change | New schema and acknowledgement needed? |
|---|---|
| jawOpen changes from 20 to 30; tracking is lost; audio becomes active/inactive | No. Reflect it in FRAME |
| Add/remove a field, rename it, or change its order | Yes. Increase schema_id |
| Change i16 to i32 | Yes. Increase schema_id |
| Change only the ScrapingValue token | No. Reflect it in the next FRAME header |

For this initial UDP design, use **pause -> new schema -> acknowledgement -> resume**.
Do not continue sending using the old schema while transitioning to the new one as a parallel, dual-stream mechanism.

```text
PC                                              iOS
 |<--- FRAME [id 7] -----------------------------| Streaming with schema 7
 |                                               |
 |                                      Settings change: add a custom field
 |                                      Pause FRAME transmission
 |<--- SCHEMA [id 8] ----------------------------| Send the new schema
 |     Validate everything; update field indices |
 |---- SCHEMA_ACK [id 8] ----------------------->| Schema 8 has been stored
 |                                               |
 |<--- FRAME [id 8] -----------------------------| Resume with the latest values
 |<--- FRAME [id 8] -----------------------------|
```

**Required sender behavior**

1. Once a settings change is committed, issue a new schema_id and make the schema, numeric type, and indices an immutable snapshot.
2. Stop UDP FRAME transmission. Discard old-schema FRAMEs/fragments that have not yet been handed to the OS. Packets already sent cannot be recalled.
3. Cache and send the new schema, then enter `WAIT_SCHEMA_ACK`. Coalesce rapid schema changes to the latest one and keep the once-per-second send limit.
4. Resume only after receiving an ACK whose session_id, source IP/port, and current schema_id all match.
   An ACK for old schema 7, future schema 9, a different session, or a different device must not start transmission using schema 8.
5. While waiting, continue face calculations and display updates, retaining only the latest numeric snapshot.
   After the ACK, resume with the latest values; do not send a backlog of FRAMEs accumulated during the wait. Do not reset sequence on a schema change.

**Further schema changes during an ACK wait**

If schema 9 becomes necessary while waiting for the ACK for schema 8, supersede schema 8 and wait only for schema 9.
Ignore a delayed ACK for schema 8. Do not mix old-layout numeric arrays into schema 9 transmission.
Within the same unacknowledged period, updates, retransmissions, HELLOs, and PINGs must not extend the 10-second deadline measured from the original start of the wait.
Do not send different payloads with the same ID. Start a new session when schema IDs are exhausted.

**Required receiver behavior**

Do not send an ACK until all fragments, start information, and JSON have been validated. An invalid schema must not overwrite the current one.
Update the saved schema's indices/numeric type before sending the ACK. Python's `on_schema` callback must finish before the ACK is transmitted.
Once a new schema is committed, discard incomplete old-schema FRAMEs and queued ACKs for the old schema. Do not interpret delayed FRAMEs carrying an older schema_id.
While assembling the new schema, it is permissible to interpret delayed old FRAMEs using the still-valid old schema, but never mix the two.
To keep displaying the last pose, retain only the already-interpreted rendering state from the old schema. Do not reinterpret an old numeric array using new indices.

TCP issues a new SCHEMA for the same kinds of changes but does not wait for an ACK.
Preserve this order on the same connection: old data already sent, then the new SCHEMA, then new FRAMEs.

## 6. What arrives afterward — FRAME

```text
[Common header 40 B] [N BlendShape values] [head rotation 3] [head position 3] [rightEye rotation 3] [leftEye rotation 3]
                        i16 / i32          <------------------ Float32 x 12 = 48 B ------------------>
```

This is the entire FRAME payload. Do not repeat the count or numeric type at the beginning; the schema already provides them.
Tracking is carried in the header flags, and source_token is also in the header. Neither belongs in the FRAME payload.

Example: schema `[eyeBlinkLeft, eyeBlinkRight, jawOpen, myCustomSmile]`, encoding i16:

```text
Name order:  eyeBlinkLeft | eyeBlinkRight | jawOpen | myCustomSmile
Values:          12      |       8       |    45   |      -25
Binary:         0c 00    |     08 00     |   2d 00 |     e7 ff
                  2 B           2 B          2 B          2 B
```

One unit is one existing integer percentage point: 25 -> normalized 0.25, -25 -> -0.25, 150 -> 1.5. Do not restrict values to 0–100.
If a value exceeds the i16 range, issue a new i32 schema **before sending it**. Do not automatically downgrade within the same session.
If it does not fit i32 either, report ERROR. Silent clamping or overflow is forbidden.
Reuse the existing rounding result from `Int(weight * 100)` / `blendShapePercent`; do not independently change calculation precision or order.

Head/eye values are Float32 values after the existing axis, scaling, calibration, and mirroring operations. Rotations are in degrees; positions are meter-derived values.
Because app settings have already been applied, these are not absolute real-world coordinates. Preserve the meaning of the profile; do not independently convert to another common axis system.
Head consists of six values, rx/ry/rz/px/py/pz. The right and left eyes each contain rx/ry/rz. All values must be finite.
Do not transmit internal values such as head[6...8]. Reject NaN/Inf and length mismatches.

When tracking is lost, set flags bit0=0. Keeping the last values is allowed, but do not falsely mark them as currently tracked. Set bit1=1 during playback.
If a recording has no tracking information, derive bit0 from the current camera tracking state; do not confuse it with the validity of the playback data.

`sequence` is a per-session UInt32. A value is newer if `(new-old) mod 2^32` is in the range 1 through 2^31-1.
Do not accept duplicates, older/out-of-order values, or a half-range difference. Gaps can include intentional sender-side drops; do not automatically classify them as network loss.
With 52 fields and i16, a FRAME is 40+104+48=192 bytes. At 60 fps, FRAME messages alone use 11520 bytes/second. IP and other network headers, schemas, and control messages are additional.

## 7. UDP — never guess the schema after losing it

**Fragment SCHEMA into datagrams of at most 576 bytes, including the FMV3 header. Fragment FRAME according to the negotiated max_udp_size.**
The SCHEMA fragment capacity is fixed in advance so that the receiver can safely reassemble it before reading the start information.
This avoids needing an extra startup-response packet or waiting for the first fragment before handling others.

```text
SCHEMA: capacity = 576 - 40 = 536
FRAME : capacity = max_udp_size - 40
part_count = max(1, ceil(total_payload_length / capacity))
```

Each fragment consists of a 40-byte header plus a payload fragment. Every fragment except the last contains `capacity` payload bytes; the last contains the remainder. The maximum is 512 fragments.
All fragments share kind/flags/session_id/schema_id/sequence/source_token/total_payload_length/part_count.
Concatenate by part_index before interpreting the payload. Do not pass an incomplete FRAME to the renderer.
The fixed 576-byte limit does not guarantee the path MTU. The protocol targets trusted LANs; use TCP or another suitable transport if a special path does not deliver these datagrams.

Allow at most 8 in-progress messages and a total of 524288 stored fragment bytes. Discard incomplete FRAME assemblies after 250 ms and incomplete SCHEMA assemblies after 3 seconds.
Ignore identical duplicates. Discard an assembly if fragments or metadata conflict. Retransmitted fragments of the same schema with the same payload may be reused.
The sender must not queue an unbounded number of copies of the same schema. Use one active schema-send operation, at most one pending resend, and finite send batches.
Prioritize the latest motion rather than accumulating old motion. Consider a lower frame rate or TCP for large UDP field counts.

### 7.1 When a schema or ACK does not arrive

| Missing data | iOS behavior | PC behavior |
|---|---|---|
| All or part of the initial SCHEMA | Send no numeric data; retransmit the same schema at 1-second intervals | Do not ACK until complete. Retransmit the same HELLO at 1-second intervals |
| All or part of an updated SCHEMA | Keep numeric transmission paused and retransmit the same schema | Do not guess new values using the old schema. ACK only after receiving the complete schema |
| SCHEMA_ACK | Send no numeric data; retransmit the same schema at 1-second intervals | ACK the saved schema again after recognizing its retransmission |
| FRAME | Do not retransmit that FRAME | Resume using the next completely received FRAME |

```text
PC                                              iOS
 |         X---- SCHEMA [id 7] ------------------| Schema lost
 |                                               | No FRAME yet
 |<--- SCHEMA [id 7] ----------------------------| Retransmit after 1 second
 |---- SCHEMA_ACK [id 7] ----X                   | ACK lost this time
 |                                               | Still no FRAME
 |<--- SCHEMA [id 7] ----------------------------| Retransmit after another second
 |---- SCHEMA_ACK [id 7] ----------------------->| Acknowledgement received
 |<--- FRAME [id 7] -----------------------------| First numeric FRAME
```

If the receiver already holds the complete schema, it may compare a retransmitted fragment with the saved payload and retransmit an ACK.
This means “I currently hold the complete schema.” It does not permit acknowledging an incomplete first receipt.
There is no need to parse identical JSON again for the same ID. Send one ACK immediately upon adopting a new schema; limit subsequent re-ACKs to at most once per second.
Do not acknowledge an ACK. Repeating the schema until iOS receives its ACK handles loss of the ACK itself.

### 7.2 Exact SCHEMA_ACK contents

```text
[Common header 40 B] [No payload]
 type=10 / session_id=the session ID from SCHEMA / schema_id=the stored schema ID
 flags=0 / sequence=0 / source_token=0
 total_payload_length=0 / part_index=0 / part_count=1 / chunk_length=0
```

Do not put the HELLO nonce in the session_id field. The PC uses the accepted server session_id.
iOS validates the source endpoint as well as the header. It must also have actually sent the schema with the acknowledged ID.
Do not repeat the names or a checksum in the ACK. The complete payload associated with a given session_id/schema_id must be immutable.
This is a synchronization acknowledgement on a trusted LAN, not cryptographic sender authentication.

### 7.3 Retransmission interval, limits, and stopping

- Start an independent UDP ACK-wait deadline at the initial schema send, and at the point when FRAME transmission is paused for a schema change.
  **If the ACK for the current schema does not arrive within 10 seconds, send `ERROR: SCHEMA_ACK_TIMEOUT` if possible and end that v3 session.**
- Retry at 1-second intervals. If the first send is possible at time 0, there are at most 10 send opportunities at 0,1,...,9 seconds; stop at 10 seconds.
  Multiple fragments of one schema count as one schema-send batch. Do not overlap batches and let the queue grow.
- All iOS UDP SCHEMA transmissions triggered by HELLO, GET_SCHEMA, or a timer share one per-peer limit of at most one schema-send batch per second. Coalesce overlapping changes to the latest schema.
  This limit applies to SCHEMA transmission/retransmission batches, not to all control packets combined. HELLO, GET_SCHEMA, SCHEMA_ACK, and PING retain their respective timing rules.
  PINGs, identical HELLOs, and retransmission of either the same or a new schema must not extend the ACK-wait deadline.
- An already-acknowledged schema may optionally be retransmitted about every 10 seconds. That periodic retransmission alone must not clear the acknowledged state or pause FRAME transmission.
  Duplicate ACKs must not reset schema/frame numbers, state, or usage time either.
- Process PING/PONG and STOP even while waiting for an ACK. Apply the session lease and existing usage-time limits independently of the ACK deadline.
  Stop when the earliest applicable deadline expires.

A PC that never receives a complete initial schema sends HELLO at most 5 times, then enters WAITING while keeping its receive endpoint open.
If the PC has the schema but its ACK does not arrive, the 10-second rule above ends that iOS send attempt. The PC does not stop listening.
Successful communication cannot be guaranteed while the network remains unavailable. Ending an unconfirmed send attempt is safer than transmitting FRAMEs without confirmation.

### 7.4 Unknown schemas and delayed data

Even with the acknowledgement mechanism, defensively discard FRAMEs carrying an unknown schema_id and send GET_SCHEMA at most once per second.
GET_SCHEMA is not a substitute for an ACK. Before the initial session_id is established, retransmit the same HELLO instead of GET_SCHEMA.
If iOS no longer holds the requested schema ID, return the latest schema. An old schema or ACK must not roll back the current schema.

## 8. TCP — one connection, with numeric data following the schema

The PC connects to the iOS direct-TCP listener, iFacialMocap:49984 or Facemotion3d:49994, and sends HELLO. iOS queues SCHEMA first, then FRAME.
**For TCP only**, do not enter a state waiting for SCHEMA_ACK. Periodic schema retransmission is unnecessary.
Leave transport-level retransmission and ordering to TCP. The receiver must finish storing the complete SCHEMA before decoding subsequent FRAMEs.
The PC does not need to send an additional GET_SCHEMA during normal startup.

Do not treat one TCP recv/receive call as one message.
Once 40 bytes are available, validate the header. Once `chunk_length` payload bytes are also available, extract the message and retain any remaining bytes for the next message.
On TCP, part_index=0, part_count=1, and chunk_length=total_payload_length.
Do not add an extra 4-byte length prefix, a legacy TCP terminator string, or application-level SCHEMA fragmentation packets.

Use one serial write queue per connection to preserve order; replace not-yet-sent motion with the latest single frame.
Do not truncate a message already being sent. Preserve the ordering between a required schema and the FRAMEs that depend on it.
Bound the number of concurrent writes and the receive buffer. Do not wait indefinitely on a stalled network operation.
For normal PC-initiated TCP startup, iOS must close a connection if it does not receive a complete HELLO within 5 seconds of accepting that connection. Discard incomplete messages on disconnect. The PC's separate deadline for receiving the initial SCHEMA is specified in Section 9.3.

## 9. FRAME-specific monitoring, bounded recovery, and indefinite waiting

### 9.1 Keep two separate timestamps

- `last_valid_receive`: the time a protocol-valid SCHEMA, PONG, or FRAME was received.
- `last_frame_at`: the time **a new FRAME was successfully validated and decoded using the current schema**.

PONGs, schemas, ACK retransmissions, partial fragments, malformed FRAMEs, and duplicate/out-of-order FRAMEs do not update last_frame_at.
A complete FRAME with tracking=false does update it. A face being out of view is not itself a network disconnection.

### 9.2 Default PC timers

| Stage | Behavior |
|---|---|
| Normal UDP startup | Send HELLO immediately, then at 1-second intervals, at most 5 sends total |
| Normal TCP startup | At most 5 connection attempts. Default connect timeout: 3 seconds per attempt; minimum attempt interval: 1 second |
| Initial schema committed | Update field indices in on_schema; ACK on UDP. Then monitor for the first FRAME |
| No first FRAME / FRAME reception stops | Start recovery attempts 3 seconds after the reference timestamp |
| Recovery attempts | At 1-second intervals, at most 3 attempts. UDP retransmits the saved schema's ACK and GET_SCHEMA(latest=0). TCP sends only GET_SCHEMA |
| No FRAME 12 seconds after the reference timestamp | Enter WAITING. Keep the receive endpoint open; stop timer-driven HELLO, ACK, GET_SCHEMA, PING, and automatic reconnection |
| No valid traffic at all for the lease period / an iOS ERROR | May enter WAITING earlier than 12 seconds. Do not terminate the program |
| Valid schema arrives while WAITING | Validate/store it and complete on_schema, then ACK on UDP. Do not request old FRAME retransmission |
| Complete new FRAME arrives while WAITING | Return to STREAMING. Reset monitoring and the three-attempt recovery budget for a subsequent interruption |

FRAME-based resumption in the table requires a schema validated for the current UDP session or the same still-open TCP connection. It does not permit a newly established TCP connection to skip its initial SCHEMA; see Section 9.3.

The initial reference timestamp is when the schema was stored. Afterward, it is the time of the last valid FRAME.
The initial ACK immediately after receipt is not one of the three recovery attempts. ACKs responding to schema retransmission are passive responses, separately limited to at most once per second.
If a timer and a schema retransmission occur in the same second, their ACKs may be coalesced. The three attempts limit transmission attempts, not guarantee delivery.

The default 12-second limit leaves room beyond iOS's independent 10-second ACK wait. Do not immediately treat an ACK wait for a schema update as a disconnection.
Within one period without valid FRAMEs, receiving a new schema_id or PONG must not restart the FRAME deadline or recovery counter.
After adopting a new schema, re-ACK only that schema. Do not leave ACKs for old schemas queued.
Once WAITING has been entered, receiving the same or a new schema must not restart timer-based recovery indefinitely. Continue replying with ACKs; only a FRAME restores normal monitoring.
A freshly started `--listen` receiver has not yet exhausted recovery, so its first manual SCHEMA starts initial-FRAME monitoring.

### 9.3 Keep the receive endpoint open while waiting

**UDP:** Continue recvfrom on the same bound socket. Do not use UDP connect to filter to one fixed peer; validate IP/session in the application.
Do not send STOP merely to enter waiting. Do not recreate the port or change it to an automatically assigned port.
A delayed valid FRAME from the same session may resume streaming if its schema and sequence can be validated.

**TCP:** Keep reading an existing connection that is still alive. On disconnection or a malformed stream, close only that connection and retain the PC TCP listener.
Accept manual mode in which iOS later connects to PC:49986 and sends SCHEMA first.
Incoming connections may be accepted while waiting, but a successful connection alone does not mean STREAMING.
Close a new connection if a complete initial SCHEMA is not received within 5 seconds, and return to the listener. Do not accept an unlimited number of parallel connections.

**A new TCP connection must establish its own schema context, even when the remote IP address and port are identical to those of a previous connection.** Track initial-SCHEMA receipt for each connection instance, rather than inferring it from a retained peer/session pair. Start the 5-second initial-SCHEMA deadline when that connection is established (when the PC accepts an incoming manual connection). A previously adopted session or incoming partial data must not bypass or restart that deadline.

Until a complete initial SCHEMA has been received and validated on that connection, and `on_schema` has completed successfully, do not decode or forward its FRAMEs and do not enter STREAMING. A matching old session_id/schema_id pair and an otherwise newer sequence are not sufficient. On disconnection, discard incomplete stream bytes and invalidate the disconnected connection's authority to validate FRAMEs on a future connection. Previously decoded rendering state and retired-session rejection history may be retained; neither substitutes for a new connection's initial SCHEMA. Manual startup still requires the new session_id specified in Section 9.4.

This does not change UDP waiting or resumption on a TCP connection that never disconnected. After any required initial-schema validation, keep the bounded recovery and receive-only waiting rules in Section 9.2; do not introduce unlimited automatic retries.

The receiver exits for explicit stop, Ctrl+C, an optional duration expiring, bind failure, callback failure, or similar reasons.
This does not guarantee recovery from PC sleep, OS termination, a disappearing network interface, or firewall blocking.
The supplied Python accepts `--no-reconnect`. With or without that option, it does not reconnect automatically forever; it retains receive waiting.

### 9.4 Manual startup from iOS — no PC HELLO needed

```text
PC: UDP49983 / TCP49986                    iOS (user enters PC IP/port)
   |                                       |
   | Waiting; no prior HELLO is required    |
   |<---- SCHEMA (client_nonce=0) ----------| New session_id / schema_id=1
   |      Validate/store all fragments     |
   |----- SCHEMA_ACK --------------------->| UDP only; use the server session_id
   |<---- FRAME ---------------------------| UDP: after ACK; TCP: after SCHEMA
   |<---- FRAME ---------------------------|
```

The manual-start marker is client_nonce=0 in the first 20 bytes of SCHEMA. The common header's session_id must still be nonzero.
iOS specifies actual_fps=1..60, max_udp_size=576..1200, lease_ms=10000 by default, and contract_revision=4.
The PC rejects a manual SCHEMA exceeding its permitted FPS/UDP limits; the defaults are 60 fps and 1200 bytes.
Use the same SCHEMA format at startup and on subsequent changes. client_nonce=0 remains unchanged for the session.
Do not generally remove nonce-mismatch validation. Explicitly allow only the manual-start value 0 as the exception.

UDP: iOS sends SCHEMA to the specified PC port and waits for ACK on the same iOS socket used to send it.
TCP: iOS connects to the PC TCP listener and writes SCHEMA followed by FRAME on that connection. Do not wait for HELLO from the PC.
For either transport, use a new session_id each time the manual-start button is pressed. Do not take over another running client or a legacy transfer.

iOS checks purchase status, time limits, and foreground requirements for manual startup as well. Use the same UDP 10-second ACK wait, retransmission, and stopping rules as for normal startup.
If no ACK arrives within 10 seconds, end that attempt and show in the UI that receipt could not be confirmed. The PC remains listening, so the user can start manually again from iOS.
Retransmission, PC re-ACKs, and GET_SCHEMA must not reset purchase/trial start times or frame numbers.

### 9.5 Keeping a session alive and stopping

While waiting for numeric data, recovering, or streaming, the PC sends PING every 3 seconds. iOS replies with PONG carrying the same sequence. The PC stops periodic PINGs in WAITING.
PING/PONG does not extend FRAME monitoring or iOS's 10-second ACK deadline. iOS expires the session if no valid PC control message arrives for the lease period.
The PC does not send STOP when entering waiting, so an old iOS session may end by lease expiry. The PC receive endpoint stays open.

Every PC control except HELLO uses the received and adopted server session_id. iOS validates that session_id and the source endpoint/TCP connection.
For a valid ERROR belonging to the current session or a matching, still-pending HELLO, display the reason and enter WAITING. An ERROR rejecting a pending HELLO uses that HELLO's client_nonce in the header's session_id field; an ERROR for an accepted session uses the iOS-generated session_id.
Apply a HELLO-rejection ERROR only to the matching, still-pending HELLO from the expected endpoint/TCP connection. Once a server session has been adopted, ignore delayed ERRORs for that earlier HELLO. Validate the ERROR payload as UTF-8 and apply the existing sender/session checks before treating it as a current-session error. Handle malformed messages according to Section 10: discard invalid UDP data; close the affected TCP connection for invalid TCP data.
STOP is for explicit PC termination; stopping can also be initiated by actions such as the iOS Stop button. Do not send STOP merely because retries have been exhausted.
Preserve existing stopping behavior for iOS lifecycle changes, purchase/trial limits, or lost permissions. v3 must not bypass these restrictions.
Do not overwrite existing saved sendPort/sendProtocol settings with temporary v3 connection information.

## 10. ScrapingValue and validation limits

ScrapingValue is not a pseudo-BlendShape. Place it **only in the FRAME header's source_token**.
Do not include it in the initial SCHEMA. Keep the schema immutable; token updates can take effect in the next FRAME.
Do not change the existing web HTML or the way its value is obtained. Convert the fetched value only when it is initialized or updated.

1. Trim only ASCII SP/TAB/CR/LF from both ends.
2. If empty or exactly lowercase `none`, use 0.
3. Otherwise, apply FNV-1a-32 to the UTF-8 bytes. Start at 2166136261. For each byte, calculate `h=((h XOR byte)*16777619) mod 2^32`.
4. Replace only a final hash value of 0 with 1.

Do not perform web access or hashing on every frame. Swift hashValue is not allowed. Preserve existing behavior such as caching successfully fetched values.
A token of 0 does not prevent data reception. This mechanism is not authentication, encryption, or protection against spoofing. Use a trusted LAN/VPN and do not expose the ports to the Internet.

Payload limits: SCHEMA262144, FRAME16432 (4096x4+48), ERROR512, HELLO8, and 0 for all other controls.
Reject unknown type/flags/header_size, oversized declared lengths, and mismatches between numeric type and payload length.
Discard invalid UDP data; close the connection for invalid TCP data. Do not search a corrupted TCP stream for the magic bytes to guess a resynchronization point.
A callback exception must terminate processing with its original cause preserved, rather than being disguised as a network failure.

## 11. Implementation acceptance criteria

The following tests are required:

- Even if only PONGs continue for more than 35 seconds, FRAME-specific monitoring enters WAITING after at most three recovery attempts.
- Do not retry indefinitely when no FRAME follows an ACK, FRAMEs stop during streaming, a new schema is awaiting confirmation, or only SCHEMA messages continue.
- In WAITING, the UDP port remains unchanged and timers send no STOP/HELLO/PING.
- ACK a subsequently received identical schema or a SCHEMA for a new manual session; resume on FRAME.
- A receiver started with `--listen` supports manual UDP startup and manual TCP connections without HELLO.
- After TCP disconnection, the PC listener remains open and iOS can connect again.
- Reconnect using the same source IP/port as a previously established TCP connection: the new connection must still enforce the 5-second initial-SCHEMA deadline. Without a complete initial SCHEMA, it must close while the PC listener stays open.
- On that new TCP connection, an old-session FRAME with a newer sequence, sent before the new connection's initial SCHEMA, must not reach the renderer or restore STREAMING. Verify separately that a valid new manual SCHEMA/session followed by FRAME can start reception.
- A matching, valid ERROR for a still-pending HELLO is handled, but a delayed ERROR for that earlier HELLO must not change an already-adopted session's state.
- Do not ACK an incomplete schema. Reject the wrong IP/nonce, retired sessions, and takeover of a running stream.

- On UDP, HELLO alone results only in SCHEMA transmission/retransmission. Do not send even one FRAME before a valid ACK.
- Recover through retransmission -> acknowledgement -> streaming both when all/part of SCHEMA is lost and when only the initial ACK is lost.
- Pause on a schema change. Do not resume for an old ID, wrong session, or different peer's ACK. Resume with the latest values only after the new ID's valid ACK.
- Test additions, removals, reordering, i16 -> i32, further changes during an ACK wait, duplicate SCHEMA/ACK, and arrival of old FRAMEs.
- PINGs, HELLO retransmission, and continuous settings changes must not extend the 10-second ACK deadline; end safely.
- On TCP, HELLO alone leads to SCHEMA -> FRAME. No application-layer ACK on that connection is needed.
- If on_schema fails, send no ACK and terminate. Reject v3 connections whose contract_revision is not 4.

Also verify negative values, values above 100, Unicode, numeric boundaries, head/eye scaling, mirroring, playback, tracking loss, stop/reconnect, purchase restrictions, and legacy v1/v2 regressions.
These are implementation/maintenance checks; this distribution does not contain the full automated test suite. `golden_vectors.json` is for byte-level comparison. For a basic exchange without an iPhone, follow the two-terminal procedure in `README.md`.
Completion of the iOS implementation also requires an Xcode build and real-device UDP/TCP interoperability tests for both apps. Do not report unperformed checks as successful.

General design references (FMV3 itself is specific to this package):
- Python struct: https://docs.python.org/3/library/struct.html
- Python Socket HOWTO: https://docs.python.org/3/howto/sockets.html
- UDP Usage Guidelines / RFC 8085: https://www.rfc-editor.org/rfc/rfc8085.html
- Transmission Control Protocol / RFC 9293: https://www.rfc-editor.org/rfc/rfc9293.html

## Appendix A. Verification notes for the supplied code snapshot

**This appendix is non-normative and is included in both language editions.** It records observations from the 2026-09-22 comparison and recheck of the supplied Python files and English diagrams. These are known implementation differences, not alternative protocol rules. This documentation revision does not modify or fix the Python code or diagrams.

The code observations below apply to the reviewed snapshot, identified by SHA-256; they are not a claim about later GitHub revisions or the production iOS implementation.

| Reviewed file | SHA-256 |
|---|---|
| `face_motion_v3.py` | `d68b145ddf7307f79146cf2d1b93f691246c215a329e89593d73c766dfd0f432` |
| `simulate_ios_v3.py` | `b4d73d14b532470820eace853c5347d059c3a6698bd3e23e40c959dff45ce9f4` |

### A.1 Reading the source and the reference code

The wire header calls the fields `message_type` and `total_payload_length`; the reference `Packet` object exposes them as `kind` and `total_length`. These are API naming differences, not different header layouts. `chunk_length` is the byte length of `Packet.payload` for an individual packet.

The startup/ACK diagrams in the opening overview describe UDP. TCP does not send an application-layer SCHEMA_ACK; TCP's own transport-level acknowledgements/retransmissions still apply. “No FRAME retransmission” in this specification means no additional application-level retransmission of old motion frames.

The transmit diagram's shorthand “HELLO only: PC client_nonce” describes PC-to-iOS messages only. For the common header across both directions, an iOS-to-PC ERROR rejecting a pending HELLO also uses client_nonce, as clarified in Sections 2 and 9.5. This requires no header size or offset change.

The repository README additionally states that iFacialMocapTr uses the iFacialMocap defaults and that Facemotion3d requires its **Other** license. These are app integration conditions, not extra JSON keys. Do not change the supported app/profile pairs in Section 5.3.

### A.2 One existing receiver validation difference

Section 3 defines ERROR payloads as UTF-8. In the supplied `face_motion_v3.py`, the pending-HELLO ERROR branch in `V3Client._handle()` uses `decode('utf-8', 'replace')`. Thus, before a server session has been adopted, an otherwise matching ERROR containing invalid UTF-8 is displayed with replacement characters and moves the receiver to WAITING rather than being rejected as malformed. The established-session ERROR path in `ReceiverCore.accept()` does reject invalid UTF-8.

This was reproduced with an ERROR payload of byte `FF`. It is an existing difference in malformed-input handling, not a translation change. Send valid UTF-8 as required by Section 3; do not copy the permissive branch as a new protocol rule. The already-fixed handling of delayed ERRORs for a previous HELLO was separately regression-tested.

### A.3 The synthetic sender is not a complete production iOS implementation

Two concrete differences were confirmed in `simulate_ios_v3.py`:

- Its FRAME loop recomputes `source_token("hapihapi")` for each generated frame. Section 10 requires a production sender to cache this result and recompute only when the source value changes. The resulting transmitted token is the same, but the simulator is not demonstrating that performance optimization.
- Its TCP accept loop does not enforce the 5-second incomplete-HELLO deadline specified in Section 8. A connection with no HELLO was still open after more than 5 seconds and accepted a later HELLO. Production iOS code must implement the specified deadline.

These differences do not alter the byte layout of valid packets or the normal startup order. They do mean that a successful simulator exchange is not proof that all production-sender requirements have been implemented. iOS lifecycle, purchase restrictions, and real-device networking must be verified separately.

### A.4 Existing receiver difference on TCP reconnection

The 2026-09-22 recheck reproduced an additional issue in `face_motion_v3.py`: after an established manual TCP connection had received SCHEMA and FRAME and then disconnected, a new connection reusing the same source IP address and port could be treated as belonging to the retained old session.

- Without sending an initial SCHEMA on the new connection, the test connection remained open for about 5.55 seconds and accepted a later SCHEMA, contrary to Section 9.3's 5-second initial-SCHEMA deadline.
- In a separate test, an old-session FRAME carrying the previous session_id/schema_id and a newer sequence was accepted on the new connection before any SCHEMA had been received there, returning the receiver to STREAMING.

The socket and TCP framing buffer were replaced, but the previous `core.session_id` and `_peer` remained. The deadline and current-session checks relied on those retained values without sufficiently distinguishing the new connection instance. The recheck forced source-port reuse on Linux; it did not establish the occurrence rate in normal use or the impact on production iOS builds.

This is a receiver implementation defect, not permission to resume an old session on an unvalidated new TCP connection. Implement connection-scoped initial-SCHEMA validation and the deadline in Section 9.3. Do not weaken the specification to reproduce this behavior. This documentation update alone does not fix the defect.

This issue concerns the **PC receiver waiting for SCHEMA on a new connection**. It is separate from the synthetic sender's **incomplete-HELLO deadline** described in A.3. It also does not prohibit resumption on the same TCP connection that has remained open.
