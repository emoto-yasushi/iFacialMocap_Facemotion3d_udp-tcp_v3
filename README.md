# Face Motion v3 — receiver sample (UDP / TCP)

Receive BlendShape values and head/eye poses from **iFacialMocap**, **iFacialMocapTr** or **Facemotion3d**.

[日本語](README_JA.md) · [Specification](PROTOCOL_V3.md) · [Changes](CHANGELOG.md) · [MIT License](LICENSE)

Pictures: [connection flows](diagrams/fmv3_flow.svg) · [message layout](diagrams/fmv3_messages.svg)

Python 3.10 or later, no `pip install`. For a real iPhone/iPad you only need **`face_motion_v3.py`**. On macOS, type `python3` instead of `python` if needed.

## 1. Receive over UDP (recommended)

Replace `PHONE_IP` with the iPhone/iPad's address on your LAN:

```sh
python face_motion_v3.py --host PHONE_IP
```

iFacialMocapTr and **Facemotion3d** use the same command. Facemotion3d needs its **"Other" or "Unity" license** (with neither, sending stops after about 10 seconds). With either license, v3 sends the "Other" output (the iFacialMocap-compatible output), so it uses iFacialMocap's ports and no extra option is needed.

It works when `State=STREAMING` appears and `frames=` keeps increasing. All values of one frame are printed on one line about once per second; every frame is still received. `--log-every 0` turns the printing off. Stop with **Ctrl+C**.

## 2. Receive over TCP

```sh
python face_motion_v3.py --host PHONE_IP --transport tcp
```

TCP works the same way. Use it when UDP is blocked on your network.

## 3. Start from the iOS app (manual start)

Start the receiver (PC) first. It waits and sends nothing:

```sh
python face_motion_v3.py --listen                    # UDP, this PC's port 49983
python face_motion_v3.py --listen --transport tcp    # TCP, this PC's port 49984
```

Then enter this PC's LAN address and the port in the app's Face Motion v3 settings and start sending. Do not enter `0.0.0.0` in the app.

The receiver (PC) never quits because the phone went quiet. After a few retries it keeps its port open (`State=WAITING`) and accepts the next start from the app.

## 4. Ports

| App | iOS listens (UDP / TCP) | This PC listens (UDP / TCP) |
|---|---|---|
| iFacialMocap, iFacialMocapTr, Facemotion3d | 49983 / 49984 | 49983 / 49984 |

`--ios-port` changes the port on the phone that the PC connects to. `--pc-port` changes the port this PC listens on. Each side uses one port per transport. If the PC port is already used by another program, the receiver (PC) stops with a message saying so. It does not switch to another port.

## 5. Supported app versions

| App | Version |
|---|---|
| iFacialMocap | 1.5.3 or later |
| iFacialMocapTr | 1.2.6 or later |
| Facemotion3d | 1.4.6 or later ("Other" or "Unity" license) |

These versions use **FMV3 contract revision 5**. Beta builds made before that used revision 4. The app and the receiver (PC) then refuse each other with a message, so update both. This is a v3-only receiver. It does not read the v1/v2 text format.

## 6. Use the values in your own program

Read values from the callbacks, not from the printed lines. Look up BlendShape names once when a schema arrives (the SCHEMA is the BlendShape name list and how to read the numbers), then read by index on every frame:

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

- The number of BlendShapes is **not fixed at 52**. It depends on the app and its settings, and the list can change during a stream, for example when playback starts of a recording with another BlendShape list (`on_schema` is called each time). All names are in `frame.schema.blend_names`, and all values are in `frame.blend_values`.
- Values below 0 or above 100 are valid. Do not clamp them in the decoder.
- `frame.tracking` is `True` while the face is tracked. Frames keep arriving while the face is lost, with `False`. With live values it shows whether the camera tracks the face now. During playback it shows the state recorded with that frame (whether the face was tracked when it was recorded), and only for an old recording without that state does it show the current camera state. `frame.playback` is `True` while a recording is played back (the `flags` in section 3.2 of the specification).
- `run()` blocks the calling thread. Keep callbacks short, and hand values to your renderer.
- Options: `transport="tcp"`, `listen_only=True`, `ios_port=`, `pc_port=`. `fps=` and `udp_size=` (`--fps`, `--udp-size`) are sent in the HELLO of a normal start. A manual start from iOS uses the app's own settings and is accepted at any valid value (1–60 fps, 576–1200 bytes).

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

Add `--transport tcp` to **both** commands for TCP. The simulator sends 3 BlendShapes and adds a 4th after 30 frames (a schema change). `--change-after 0` keeps one schema, and `--count 52` sends 52. Its fault options (`--drop-first-schema`, `--mute-after`, …) test the recovery of a receiver (PC). A simulated exchange does not replace a test with the real app.

## 8. Troubleshooting

- **"port … is already in use"**: another receiver program or the simulator is running on that port. Close it or pick another `--pc-port`. Right after a TCP connection closes, the OS can keep the port busy for up to a minute.
- **`iOS refused the request: 'UNSUPPORTED_CONTRACT: requires 4'`**, or a message saying a SCHEMA looks like one from a revision-4 app: the app is an old beta (revision 4). Update the app.
- **`iOS refused the request: 'BUSY'`** (or another text): the app declined to start. See the list of reasons in the specification, [section 3.8](PROTOCOL_V3.md#error).
- **No values**: check the phone's address, the transport, the Wi-Fi network, and the PC firewall for the ports in section 4. In Facemotion3d, also check that "Settings → Other functions → No connection accepted from PC" is off (while it is on, TCP connections fail).
- When reporting a problem, include the app and version, the device, the command, and the last `State=` line.

FMV3 has no authentication or encryption. Use it on a trusted LAN, and never expose these ports to the Internet.

## 9. Why v3?

The v1/v2 protocols are **text**: every frame repeats all BlendShape names with their values. They are easy to read and easy to start with. [Official v1/v2 documentation](https://www.ifacialmocap.com/for-developer/)

v3 started from an embedded developer whose device spent more CPU time parsing these strings than rendering. In v3 a **SCHEMA** sends the list of BlendShape names (in order) and the number type once. Every **FRAME** then carries only binary numbers in the order of that list. The list is fixed *per schema*, not forever: BlendShapes can be added or renamed, and custom BlendShapes are included.

| | v1/v2 (text) | v3 (schema + binary) |
|---|---|---|
| Reading the data by eye | Easy | Needs a decoder (this sample prints decoded values) |
| First working receiver | Usually quicker | More work: schema, validation, ACK and recovery |
| Work per frame | Split strings, look up names, convert text | Copy numbers by index |
| Data per frame | Names + text numbers | Numbers only |

**Less bandwidth.** 52 BlendShapes as `i16` take **192 bytes per FRAME** (40 header + 104 values + 48 head/eyes). That is **11,520 bytes/s at 60 fps** for FRAMEs. This number is calculated, not measured. Schema exchange, control messages and network headers add a little. Less data does not by itself guarantee lower latency or a higher frame rate.

**Embedded receivers.** v3 removes repeated text parsing, but a receiver (PC) still needs schema storage, fragment reassembly, validation and the UDP acknowledgement. Measure CPU and memory on your target. The Python files are a reference and a test tool. Implement the [specification](PROTOCOL_V3.md) in C, C++ or any other language, and use `golden_vectors.json` to check your bytes.

v3 is an additional option. A working v1/v2 integration does not need to change.

## 10. License

The code and documents in this repository are released under the **[MIT License](LICENSE)**. You may use, modify and redistribute them, including commercially, as long as you keep the copyright and permission notices.

The license covers this repository, **not the iOS apps**. The apps' own purchase and license terms still apply. Using this interface with Facemotion3d requires its **"Other" or "Unity" license**.
