# Face Motion v3 — receiver sample (UDP / TCP)

Receive BlendShape values, head rotation and position, and eye rotation from **iFacialMocap**, **iFacialMocapTr** or **Facemotion3d**.

[日本語](README_JA.md) · [Specification](PROTOCOL_V3.md) · [Changes](CHANGELOG.md) · [MIT License](LICENSE)

Pictures: [connection flows](diagrams/fmv3_flow.svg) · [message layout](diagrams/fmv3_messages.svg)

Python 3.10 or later, no `pip install`. For a real iPhone/iPad you only need **`face_motion_v3.py`**. On macOS, type `python3` instead of `python` if needed.

## 1. Receive over UDP (recommended)

Replace `PHONE_IP` with the iPhone/iPad's address on your LAN:

```sh
python face_motion_v3.py --host PHONE_IP
```

iFacialMocapTr and **Facemotion3d** use the same command. Facemotion3d needs its **"Other" or "Unity" license** (with neither, sending stops after about 10 seconds). With either license, v3 sends the "Other" output (the iFacialMocap-compatible output), so it uses iFacialMocap's ports and no extra option is needed.

It works when `State=STREAMING` appears and `frames=` keeps increasing. All values of one frame are printed on one line about once per second (every frame is received). `--log-every 0` turns the printing off. Stop with **Ctrl+C**.

**Two modes.** The receiver (PC) selects the mode with `--mode`. When this command starts the stream (normal start), the selected mode is passed to the app as it is.

| | Variable mode (default) | Fixed52 mode |
|---|---|---|
| BlendShapes you receive | Everything the app sends, custom BlendShapes included. The number and names depend on the app and its settings | Only the 52 standard ARKit values, always in the same order and position |
| Names | A name list arrives first. The list can change during a stream | Not sent (the order is fixed by the [specification](PROTOCOL_V3.md#fixed52-names)) |
| Work for the receiver (PC) | Read the name list and handle changes of the list | Read fixed positions |

To receive in fixed52 mode:

```sh
python face_motion_v3.py --host PHONE_IP --mode fixed52
```

<details>
<summary>Details: what this sample does for you (PING and SCHEMA_ACK)</summary>

In both modes the receiver (PC) sends a PING (a short message that tells iOS the receiver keeps receiving) every 3 seconds, over UDP and TCP. Over UDP it also answers the SCHEMA that iOS sends first (how to read the numbers and, in variable mode, the name list) with a SCHEMA_ACK (a reply saying "received"). This sample does both.

</details>

## 2. Receive over TCP

```sh
python face_motion_v3.py --host PHONE_IP --transport tcp
```

TCP works the same way. Use it when UDP is blocked on your network.

## 3. Start from the iOS app (manual start)

Start the receiver (PC) first. It waits and sends nothing. To receive in fixed52 mode, add `--mode fixed52` to the command you use:

```sh
python face_motion_v3.py --listen                    # UDP, this PC's port 49983
python face_motion_v3.py --listen --transport tcp    # TCP, this PC's port 49984
```

Then set this PC's LAN address in the app, select the same mode as the receiver (PC), and start sending. Do not enter `0.0.0.0` in the app.

- **iFacialMocap, iFacialMocapTr**: Settings → enter the address in "Destination IP address" in the "Destination setting" section. In "Detail Settings" of the same section, set "Protocol" to "Face Motion v3 UDP" ("Face Motion v3 TCP/IP" for TCP) and choose the mode in "Face Motion v3 mode". Sending starts when the tracking screen is shown.
- **Facemotion3d**: Settings → "Streaming settings": set "Software Name" to "Other", "Sending Format" to "Face Motion v3" and "Communication Method" to "UDP" ("TCP/IP" for TCP), and choose the mode with the button at the top right ("v3: Variable" or "v3: ARKit 52"). Sending starts when you choose "Other" with the Live button, enter the address and press "Connect".

The names of the choices are in the "Manual start" table in [section 2](PROTOCOL_V3.md#udp) of the specification. The mode setting is used only for a manual start. In a normal start, the receiver's `--mode` reaches the app in the HELLO (the start request that the receiver (PC) sends first).

If the modes differ, the receiver (PC) prints `SCHEMA mode does not match the requested receiver mode` and does not accept that SCHEMA. Over UDP it sends no SCHEMA_ACK, so the app stops sending after about 10 seconds (`SCHEMA_ACK_TIMEOUT`). Over TCP the receiver (PC) closes the connection.

The receiver (PC) never quits because the phone went quiet. After a few retries it keeps its port open (`State=WAITING`) and accepts the next start from the app.

## 4. Ports

| App | iOS listens (UDP / TCP) | This PC listens (UDP / TCP) |
|---|---|---|
| iFacialMocap, iFacialMocapTr, Facemotion3d | 49983 / 49984 | 49983 / 49984 |

`--ios-port` changes the port on the phone that the PC connects to. The apps always listen on the ports in the table, so `--ios-port` is for the simulator (section 7) and similar senders. `--pc-port` changes the port this PC listens on; if you change it, enter the same port in the app's manual-start destination. Each side uses one port per transport. If the PC port is already used by another program, the receiver (PC) stops with a message saying so. It does not switch to another port.

## 5. Supported app versions

| App | Version |
|---|---|
| iFacialMocap | 1.5.3 or later |
| iFacialMocapTr | 1.2.6 or later |
| Facemotion3d | 1.4.6 or later ("Other" or "Unity" license) |

These versions use **FMV3 contract revision 6** (the version number of the protocol). Revisions 5 and 4 were used only by beta builds before the App Store release. An app and a receiver (PC) with different revisions do not work together, so update both (see [compatibility](PROTOCOL_V3.md#compat) in the specification). This is a v3-only receiver. It does not read the v1/v2 text format.

## 6. Use the values in your own program

Read values from the callbacks, not from the printed lines. Look up BlendShape names once when a table arrives (the SCHEMA: how to read the numbers and, in variable mode, the BlendShape name list), then read by index on every frame:

```python
from face_motion_v3 import V3Client

jaw = None

def on_schema(schema):
    global jaw
    jaw = schema.index_by_name.get("jawOpen")      # names can change; look up again here

def on_frame(frame):
    if jaw is not None and frame.tracking:
        value = frame.blend_values[jaw] / 100.0    # integer percent: -25 -> -0.25
        # frame.head = (rx, ry, rz, px, py, pz); frame.right_eye / frame.left_eye = (rx, ry, rz)

V3Client("PHONE_IP").run(on_frame, on_schema=on_schema)
```

- **Variable mode** (default): the number and names of BlendShapes depend on the app and its settings. The list can change during a stream, for example when playback starts of a recording with another BlendShape list (`on_schema` is called each time). All names are in `frame.schema.blend_names`, and all values are in `frame.blend_values` (`schema.mode` is `"variable"`).
- **Name spelling** (variable mode): every app sends the standard 52 in the `eyeBlink_L` spelling (the string returned by ARKit; the "String returned by ARKit" column of the [fixed52 table](PROTOCOL_V3.md#fixed52-names) in the specification). Names marked (same) in that column, such as `jawOpen` in the example, `jawLeft` and `mouthLeft`, stay as they are. Facemotion3d sends the replaced names if its name-replacement setting is used, and also sends its own names such as `FM_SAD` and the voice-input names.
- **Fixed52 mode**: the [52 standard ARKit values](PROTOCOL_V3.md#fixed52-names) always arrive in the same order as Int32 (4-byte signed integers) (`schema_id = 1`). A BlendShape the app does not have is 0, and custom BlendShapes are not sent. The values include the app's adjustments, such as scaling and calibration. Names are not sent, so this sample puts the 52 standard names in `frame.schema.blend_names` (`schema.mode` is `"fixed52"`, and `app` and `profile` are empty strings). These names use the identifier spelling, such as `eyeBlinkLeft`, not the `eyeBlink_L` of variable mode.
- Values below 0 or above 100 are valid. Do not clamp them in the decoder.
- `run()` blocks the calling thread. Keep callbacks short, and hand values to your renderer.

<details>
<summary>Details: tracking and playback values, options, settings in a manual start</summary>

- `frame.tracking` is `True` while the face is tracked. Frames keep arriving while the face is lost, with `False`. Both live and during playback, it shows whether the camera tracks the face now (the apps do not store the tracking state in recordings). `frame.playback` is `True` while a recording is played back (the [`flags` in the header](PROTOCOL_V3.md#header) in the specification).
- Options: `mode="variable"` (default) or `mode="fixed52"`, `transport="tcp"`, `listen_only=True`, `ios_port=`, `pc_port=`. Fixed52 example: `V3Client("PHONE_IP", mode="fixed52").run(on_frame, on_schema=on_schema)`.
- `fps=` and `udp_size=` (`--fps`, `--udp-size`) are sent in the HELLO of a normal start. A manual start uses the app's own settings, and the receiver (PC) accepts them within the valid ranges (1–60 fps, 576–1200 bytes). Only the mode must match the receiver (PC) (section 3).

</details>

## 7. Try it without an iPhone

`simulate_ios_v3.py` pretends to be the iOS app with synthetic values (not ARKit). Keep it next to `face_motion_v3.py`, and use two terminals in that folder.

**Normal start.** On one PC, the simulated phone needs its own port, here 51083:

```sh
python simulate_ios_v3.py --ios-port 51083                      # terminal 1
python face_motion_v3.py --host 127.0.0.1 --ios-port 51083      # terminal 2
```

**Manual start.** The simulated phone sends first:

```sh
python face_motion_v3.py --listen                               # terminal 1
python simulate_ios_v3.py --push-host 127.0.0.1                 # terminal 2
```

<details>
<summary>Details: testing fixed52, TCP, recovery and more</summary>

**Fixed52.** For a normal start, add `--mode fixed52` to the receiver (PC) only (the HELLO selects the simulator's mode). For a manual start, add it to both:

```sh
# normal start
python simulate_ios_v3.py --ios-port 51083                                    # terminal 1
python face_motion_v3.py --host 127.0.0.1 --ios-port 51083 --mode fixed52     # terminal 2
# manual start
python face_motion_v3.py --listen --mode fixed52                              # terminal 1
python simulate_ios_v3.py --push-host 127.0.0.1 --mode fixed52                # terminal 2
```

In variable mode the simulator sends 3 BlendShapes and adds a 4th after 30 frames (a table change). In fixed52 mode it sends the 52 standard values and does not use `--count` or `--change-after`.

| To test | Add |
|---|---|
| TCP | `--transport tcp` to **both** commands |
| One table only (variable mode) | `--change-after 0` to the simulator |
| 52 synthetic names in variable mode | `--count 52` to the simulator (this does not select fixed52) |
| Recovery of the receiver (PC) | a fault option to the simulator (`--drop-first-schema`, `--mute-after`, …) |

</details>

A simulated exchange does not replace a test with the real app.

## 8. Troubleshooting

- **"port … is already in use"**: another receiver program or the simulator is running on that port. Close it or pick another `--pc-port`. Right after a TCP connection closes, the OS can keep the port busy for up to a minute.
- **`SCHEMA mode does not match the requested receiver mode`**: in a manual start, the app and the receiver (PC) use different modes. Set the app's mode (section 3) to the receiver's `--mode`.
- **`iOS refused the request: 'BUSY'`** (or another text, such as `UNSUPPORTED_MODE`): the app declined to start. See the list of reasons in the [ERROR section](PROTOCOL_V3.md#error) of the specification.
- **No values**: check the phone's address, the transport, the Wi-Fi network, and the PC firewall for the ports in section 4. In Facemotion3d, also check that "Settings → Other functions → No connection accepted from PC" is off (while it is on, TCP connections fail).
- When reporting a problem, include the app and version, the device, the command, and the last `State=` line.

<details>
<summary>Details: a revision mismatch is reported</summary>

- **Revision mismatch**: if a manual start prints `iOS uses FMV3 contract revision 5; this receiver needs revision 6`, the app is an old beta (revision 5). If a normal start receives nothing and ends in `State=WAITING`, an old beta is one possible cause. A revision-5 app drops the new HELLO as malformed (over TCP it closes the connection), so no reason comes back. Update the app.

</details>

FMV3 has no authentication or encryption. Use it on a trusted LAN, and never expose these ports to the Internet.

## 9. Why v3?

The v1/v2 protocols are **text**: every frame repeats all BlendShape names with their values. They are easy to read and easy to start with. [Official v1/v2 documentation](https://www.ifacialmocap.com/for-developer/)

v3 started from an embedded developer whose device spent more CPU time parsing these strings than rendering. In v3 a **SCHEMA** sends the number type and, in variable mode, the list of BlendShape names (in order) once. Every **FRAME** then carries only binary numbers in that order, plus the head and eye values. In variable mode (default) the list is fixed *per table*, not forever: when the list changes, a new table arrives, so BlendShapes can be added or renamed and custom BlendShapes are included. In fixed52 mode no name list is sent, and the 52 standard ARKit values are always in the same position (table in section 1).

<details>
<summary>Details: comparison with v1/v2, and bandwidth</summary>

| | v1/v2 (text) | v3 (table + binary) |
|---|---|---|
| Reading the data by eye | Easy | Needs a decoder (this sample prints decoded values) |
| First working receiver | Usually quicker | Variable: storing the table and joining a SCHEMA that arrives in parts. Fixed52: read fixed positions. Both: validation, UDP SCHEMA_ACK, PING and recovery |
| Work per frame | Split strings, look up names, convert text | Copy numbers by index |
| Data per frame | Names + text numbers | Numbers only |

**Less bandwidth.** 52 BlendShapes as `i16` (2-byte signed integers) take **192 bytes per FRAME** (40 header + 104 values + 48 head/eyes). That is **11,520 bytes/s at 60 fps** for FRAMEs. This number is calculated, not measured. The table exchange, control messages and network headers add a little. Less data does not by itself guarantee lower latency or a higher frame rate.

</details>

**Embedded receivers.** Variable mode needs storage for the name list and joining a SCHEMA that arrives in parts. Fixed52 needs neither: its SCHEMA is 66 bytes total and its FRAME is 296 bytes total. Each arrives in one piece. Fixed52 FRAMEs take 17,760 bytes/s at 60 fps (calculated, without control messages and network headers). Both modes need validation, the UDP SCHEMA_ACK and PING. Measure CPU and memory on your target. The Python files are a reference and a test tool. Implement the [specification](PROTOCOL_V3.md) in C, C++ or any other language. To check your bytes, compile your code with the answer key in C99 (`golden_vectors.h`, `golden_vectors.c`) and compare with its vectors. `golden_vectors_check.c` does this for its own encoder and decoder and needs only a C compiler: `cc -std=c99 -o golden_vectors_check golden_vectors.c golden_vectors_check.c && ./golden_vectors_check`.

v3 is an additional option. A working v1/v2 integration does not need to change.

## 10. License

The code and documents in this repository are released under the **[MIT License](LICENSE)**. You may use, modify and redistribute them, including commercially, as long as you keep the copyright and permission notices.

The license covers this repository, **not the iOS apps**. The apps' own purchase and license terms apply. Using this interface with Facemotion3d requires its **"Other" or "Unity" license**.
