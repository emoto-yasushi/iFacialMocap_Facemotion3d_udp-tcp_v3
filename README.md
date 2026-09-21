# Face Motion v3 — two-file sample

Receive BlendShape values and head/eye poses from **iFacialMocap**, **iFacialMocapTr**, or **Facemotion3d**, over UDP or TCP.

[日本語の説明](README_JA.md) · [MIT License](LICENSE)

## 1. Connect and receive — start here

**Python 3.10+; no `pip install` required.** For a real iPhone/iPad, only **`face_motion_v3.py`** is needed. Open a terminal in its folder; do **not** start the simulator. Use `python3` instead of `python` on macOS if needed.

Replace `PHONE_IP` with the iPhone/iPad's LAN address. **Run one of these commands, not both:**

```sh
# UDP
python face_motion_v3.py --host PHONE_IP --transport udp

# OR: TCP
python face_motion_v3.py --host PHONE_IP --transport tcp
```

**iFacialMocapTr uses the same commands. For Facemotion3d, the “Other” license is required; add `--app facemotion3d`.** Other Facemotion3d license types do not enable communication with this sample.

The receiver sends HELLO and handles the UDP schema ACK automatically. Normal TCP needs no application-layer schema ACK.

**Success:** `State=STREAMING` appears and `frames=` increases. One frame's **complete** values are shown on one line about once per second; every valid new frame is processed. The terminal may visually wrap the line. Stop with **Ctrl+C**. `--log-every 0` disables frame logs, not reception.

### Supported app versions

| App | Minimum supported version |
|---|---|
| **iFacialMocap** | **1.5.3 or later** |
| **iFacialMocapTr** | **1.2.6 or later** |
| **Facemotion3d** | **1.4.6 or later** |

**Only the versions listed above and later versions are supported. Earlier versions of each app are not supported.** Update the app before using this sample if its version is below the listed minimum.

The iOS app must support **FMV3 `contract_revision = 4` on the standard ports**. This is a **v3-only** receiver: it does not decode or automatically fall back to v1/v2 text packets.

## 2. Try it without an iPhone — two terminals

| File | When to use it |
|---|---|
| **`face_motion_v3.py`** | The receiver. This file **alone** can communicate with a compatible iPhone/iPad. |
| **`simulate_ios_v3.py`** | An optional synthetic sender for testing without an iPhone. Keep it beside `face_motion_v3.py`. |

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
| iFacialMocap / iFacialMocapTr | 49983 | 49983 | 49984 | 49986 |
| Facemotion3d | 49993 | 49983 | 49994 | 49986 |

`--port` overrides the iOS/simulator destination; `--listen-port` overrides the receiver's local port. Normal TCP uses one PC-initiated connection in both directions; PC49986 is for manual incoming connections, not a second reply connection.

Do not launch two receivers on the same local port. Facemotion3d requires the “Other” license; add `--app facemotion3d` to the receiver command.

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

For Facemotion3d (with the “Other” license), pass `app="facemotion3d"` to `V3Client`. iFacialMocapTr uses the default app setting.

## Reference and troubleshooting

[Wire specification (Japanese)](PROTOCOL_V3.md) · [Transmit diagram](diagrams/FMV3_PC_to_iOS_EN.png) · [Receive diagram](diagrams/FMV3_iOS_to_PC_EN.png) · [Diagram text (English)](diagrams/DIAGRAM_TEXT_EN.md) · [Expected byte vectors](golden_vectors.json)

The documents, diagrams and byte vectors are **not runtime dependencies**. The byte vectors are optional answer keys for another-language implementation. Maintainer tests and old result logs are deliberately not included in this small sample; the simulated exchange is not a substitute for real-device testing.

If a port is already in use, close the other receiver normally before retrying. If no values arrive, check the iOS build, IP, transport and firewall. When reporting a problem, include the app/build, device/OS, command and last successful stage (`SCHEMA`, `WAIT_FRAME`, `STREAMING`, or `RECOVERING`).

Use a trusted LAN only: FMV3 has no authentication or encryption. Do not expose these ports to the public Internet.

## Background: why v3 was created

The earlier **v1/v2 protocols are text-based**: each frame carries field names and values as readable text. This makes the format easy to inspect and a basic receiver relatively straightforward to write.

v3 grew out of feedback from an embedded developer whose device spent more CPU time parsing incoming strings than rendering. The goal was to reduce repeated text parsing and transmitted field names, leaving more resources for the application—not to make the receiver's overall implementation simpler.

In v3, a **`SCHEMA` defines the field names, order, and numeric types**. Subsequent `FRAME` payloads carry binary numeric values in that order. The receiver resolves names when it receives or updates the schema, rather than splitting strings, looking up names, and converting text to numbers for every frame. The order is fixed **within each schema**, not permanently: fields can be added or changed, and the item count is not fixed at 52.

## Choosing v1/v2 or v3

[Official v1/v2 communication documentation — iFacialMocap](https://www.ifacialmocap.com/for-developer/)

| Consideration | v1/v2 — text | v3 — schema + binary values |
|---|---|---|
| Inspecting received data | Names and values are readable directly in the text. | Decode the binary data using the schema; this sample prints the decoded values. |
| Getting a basic receiver working | Usually the more intuitive starting point for a small script or prototype. | More implementation work: schema management, binary validation, and transport-specific control. |
| Continuous reception | Repeats field names and text-to-number parsing each frame. | Avoids repeated field names in FRAME payloads and per-frame text parsing. |
| Bandwidth during continuous streaming | Repeats field names and text-formatted values in each frame. | Typically fewer motion-data bytes for the same field set and frame rate. |
| Good fit | Readable debugging, simple integrations, or existing projects where parsing and bandwidth are not bottlenecks. | Embedded receivers, custom renderers, or continuous streams where parsing CPU time or bandwidth is a bottleneck. |

**v3 is an additional option, not a requirement to migrate a working v1/v2 integration.** Simpler code and directly readable data can be more useful than binary efficiency. This repository implements **v3 only**; it does not decode or fall back to v1/v2. An existing v1/v2 receiver needs explicit v3 support to receive v3 data.

### Smaller motion frames, less bandwidth

For a typical continuous stream carrying the same fields at the same frame rate, **v3's binary motion frames are smaller than the equivalent v1/v2 text frames**, so less motion data needs to be sent and received and the stream uses less network bandwidth. Names and numeric types are sent in the schema rather than repeated in every FRAME.

For example, **52 BlendShapes encoded as `i16` use 192 bytes per unfragmented FMV3 FRAME**: 40 bytes of header + 104 bytes of BlendShape values + 48 bytes of head/eye values. At 60 frames per second, that is **11,520 bytes/s (11.52 kB/s) for FRAME messages alone**. This is a calculation from the [wire layout](PROTOCOL_V3.md), not a measured performance benchmark.

Network headers, schema exchange/updates/retries, and control messages such as ACK and PING/PONG add traffic. Savings depend on field names, text formatting, field count, and stream duration; **not every message or short session is guaranteed to be smaller**, and reduced bandwidth does not guarantee lower latency or a particular frame rate.

### Using v3 in an embedded environment

v3 can help resource-constrained receivers by reducing repeated work in the per-frame path. **It does not guarantee lower total memory use, a smaller implementation, or a particular frame rate on every device.** The receiver still needs to parse and store the schema, manage receive/reassembly buffers, and implement validation, schema updates, and the required communication control—including schema acknowledgements on UDP. Measure CPU time and memory use on the target hardware.

The Python files are a **reference implementation and a test tool**, not a requirement to run Python on an embedded device. You can implement the same protocol in C, C++, or another suitable language using [the wire specification](PROTOCOL_V3.md). Choose v3 when the reduced per-frame work is worth its additional protocol handling; v1/v2 may be the simpler choice when implementation size or ease of debugging matters more.

## License

The source code and accompanying documentation in this repository are released under the **[MIT License](LICENSE)**. Commercial use, modification, redistribution, and integration into other projects are permitted, provided that the copyright and permission notices are retained as required by the license. The software is provided without warranty; see `LICENSE` for the complete terms.

This license covers the repository materials, **not the iOS apps themselves**. The apps' separate purchase and licensing requirements remain unchanged. In particular, using this interface with Facemotion3d requires its **“Other” license**; this does not restrict the MIT-licensed sample code's reuse.
