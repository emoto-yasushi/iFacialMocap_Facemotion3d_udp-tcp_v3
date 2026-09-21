# Face Motion v3 — two-file sample

Receive BlendShape values and head/eye poses from **iFacialMocap**, **iFacialMocapTr**, or **Facemotion3d**, over UDP or TCP.

[日本語の説明](README_JA.md)

## Supported app versions

| App | Minimum supported version |
|---|---|
| **iFacialMocap** | **1.5.3 or later** |
| **iFacialMocapTr** | **1.2.6 or later** |
| **Facemotion3d** | **1.4.6 or later** |

**Only the versions listed above and later versions are supported. Earlier versions of each app are not supported.** Update the app before using this sample if its version is below the listed minimum.

| File | When to use it |
|---|---|
| **`face_motion_v3.py`** | The receiver. This file **alone** can communicate with a compatible iPhone/iPad. |
| **`simulate_ios_v3.py`** | An optional synthetic sender for testing without an iPhone. Keep it beside `face_motion_v3.py`. |

**Python 3.10+; no `pip install` required.** Extract the ZIP and open a terminal in this folder. Use `python3` instead of `python` on macOS if needed. Choose UDP **or** TCP; do not launch duplicate receivers on the same local port.

## 1. Receive from a real iPhone/iPad

The iOS app must support **FMV3 `contract_revision = 4` on the standard ports**. This sample does not decode legacy v1/v2 text packets. Do **not** run the simulator for a real-device connection.

Replace `PHONE_IP` with the iPhone/iPad's LAN address:

```sh
python face_motion_v3.py --host PHONE_IP --transport udp
# For TCP, use --transport tcp instead.
```

For Facemotion3d, add `--app facemotion3d`. The receiver sends HELLO and handles the UDP schema ACK automatically.

**Success:** `State=STREAMING` appears and `frames=` increases. One frame's **complete** values are shown on one line about once per second; every valid new frame is processed. The terminal may visually wrap the line. Stop with **Ctrl+C**. `--log-every 0` disables frame logs, not reception.

## 2. Try it without an iPhone — two terminals

Open **two terminals in the same folder**, and run them in this order.

**Terminal 1 — synthetic sender**

```sh
python simulate_ios_v3.py --transport udp --port 51083 --count 52 --change-after 0
```

**Terminal 2 — receiver**

```sh
python face_motion_v3.py --host 127.0.0.1 --transport udp --port 51083
```

For TCP, replace `udp` with `tcp` in **both** commands. Stop **both** programs with Ctrl+C when finished.

`127.0.0.1` means this computer. **51083 is only a simulator test port**, not a new iOS default. It avoids a bind conflict when the sender and receiver run on one computer. The receiver still uses its normal local port: UDP49983 or the TCP49986 manual listener.

This demo sends **52 synthetic fields**, not the 52 standard ARKit names. `jawOpen` changes; many other values are intentionally constant. `count=52` confirms the received value count. `--change-after 0` keeps the schema unchanged. Remove that option to test a schema change: the default adds one custom field after 30 frames.

The simulator is **not an iOS/ARKit emulator** and does not use a camera. Its ACK/retry code is included in the same file; no `schema_delivery.py` is needed.

## 3. Start manually from the iOS app

Run the receiver first, then enter the **PC's LAN IP and receiving port** in the iOS app's v3 manual-send settings:

```sh
python face_motion_v3.py --listen --transport udp   # PC UDP49983
# OR
python face_motion_v3.py --listen --transport tcp   # PC TCP49986
```

Do not enter `0.0.0.0` as the destination IP. UDP replies with a schema ACK; TCP uses the accepted connection without an application-layer ACK. The receiver remains listening after bounded recovery attempts.

| App | iOS UDP | PC UDP | iOS direct TCP | PC manual TCP |
|---|---:|---:|---:|---:|
| iFacialMocap | 49983 | 49983 | 49984 | 49986 |
| Facemotion3d | 49993 | 49983 | 49994 | 49986 |

`--port` overrides the iOS/simulator destination; `--listen-port` overrides the receiver's local port. Normal TCP uses one PC-initiated connection in both directions; PC49986 is for manual incoming connections, not a second reply connection.

## 4. Use values in your own project

Import `V3Client` from `face_motion_v3`; use callbacks and numeric fields, **not parsed console logs**. Resolve field indices when the schema arrives; use those indices for each frame. For example:

```python
from face_motion_v3 import V3Client

jaw_index = None

def on_schema(schema):
    global jaw_index
    jaw_index = schema.index_by_name.get("jawOpen")

def on_frame(frame):
    if jaw_index is not None and frame.tracking:
        jaw = frame.blend_values[jaw_index] / 100.0
        # Pass jaw to your renderer; do not block the receiving loop.

V3Client("PHONE_IP", transport="udp").run(on_frame, on_schema=on_schema)
```

The item count is **not fixed at 52**. Names are in `frame.schema.blend_names`; all values are in `frame.blend_values`. Head/eye values are `frame.head`, `frame.right_eye`, and `frame.left_eye`. Signed BlendShape integers use percentage points: `-25` → `-0.25`; do not clamp negative values or values above 100 in the decoder. `.run()` blocks its calling thread; GUI apps should manage threading and render updates appropriately.

## Reference and troubleshooting

[Wire specification (Japanese)](PROTOCOL_V3.md) · [Transmit diagram](diagrams/FMV3_PC_to_iOS_EN.png) · [Receive diagram](diagrams/FMV3_iOS_to_PC_EN.png) · [Diagram text (English)](diagrams/DIAGRAM_TEXT_EN.md) · [Expected byte vectors](golden_vectors.json)

The documents, diagrams and byte vectors are **not runtime dependencies**. The byte vectors are optional answer keys for another-language implementation. Maintainer tests and old result logs are deliberately not included in this small sample; the simulated exchange is not a substitute for real-device testing.

If a port is already in use, close the other receiver normally before retrying. If no values arrive, check the iOS build, IP, transport and firewall. When reporting a problem, include the app/build, device/OS, command and last successful stage (`SCHEMA`, `WAIT_FRAME`, `STREAMING`, or `RECOVERING`).

Use a trusted LAN only: FMV3 has no authentication or encryption. Do not expose these ports to the public Internet.

**License:** reuse terms have not yet been selected by the maintainer. No new license is granted by this packaging change.
