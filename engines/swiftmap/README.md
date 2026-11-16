# SwiftMap

SteelEagle plugins for mapping with a [SwiftMap](https://github.com/cmusatyalab/swiftmap) server.

```
vehicle:  camera → preprocess → reachback ══► remote:  swiftmap → SwiftMap server
                                   ↑                      │
                                   └─ NFN status ─────────┘  (reachback subscribes to swiftmap)
```

| Plugin | Runs on | Code | Does |
| --- | --- | --- | --- |
| `preprocess` | vehicle | `onboard.py` | Shrinks each frame to the VGGT-Omega model input and sends lossless WebP |
| `reachback` | vehicle | `reachback.py`, `onboard.py` | Forwards keyframes; sends back stored frames covering a plan's weak targets |
| `swiftmap` | remote | `remote.py` | Sends frames to the server; turns its plans into reachback requests |

`protocol.py` holds what both sides share; `main.py` starts the plugin eagled names. Each arg's default lives only in its `.plugin.toml`.

## `preprocess`

Does VGGT-Omega's preprocessing on board: center-crop an aspect ratio outside [0.5, 2], then PIL-bicubic resize to the `balanced` shape, whole `patch_size` patches about (`image_resolution`/`patch_size`)² in all (688×384 for 16:9 at 512). Sent lossless, the server's own preprocessing changes nothing, so the model input is exactly what it would make from the full frame, at about a quarter of the bytes. `image_resolution` and `patch_size` must match the server's `VGGT_OMEGA_IMAGE_RESOLUTION`. WebP goes as `CODEC_UNSPECIFIED` (the protocol has no WebP codec yet) and is recognised by its RIFF header.

| Arg | Default | |
| --- | --- | --- |
| `image_resolution` | `512` | The server's `VGGT_OMEGA_IMAGE_RESOLUTION` |
| `patch_size` | `16` | Model patch size, px |
| `lossless` | `true` | `false` for lossy WebP at `quality` |
| `quality` | `90` | WebP quality when not lossless |
| `max_output_size` | `6237184` | Largest frame sent, bytes |

## `reachback`

**Keyframes.** A frame is forwarded if it was taken at least `send_distance` m from the last one forwarded (no GPS fix fails, unless 0) and is a keyframe by the server's optical-flow test: the last keyframe's features moved more than `min_disparity` px on average. Each camera and session is tracked on its own.

**Store.** One frame every 0.2 s per camera, plus every keyframe, with its GPS and gimbal yaw/pitch, in SQLite (newest 6000).

**Reach back.** For each new request in the NFN status, it picks one frame per target, for up to `count` targets: the unsent stored frame seeing it nearest the view's centre, within 25° and 50 m. A target no frame covers gets none. It sends them one at a time, in place of incoming frames as `swiftmap/ReachbackFrame`, then a `swiftmap/ReachbackDone`. While the status says hold, nothing is forwarded.

| Arg | Default | |
| --- | --- | --- |
| `remote` | `swiftmap` | Service name of the `swiftmap` engine |
| `send_distance` | `5.0` | Meters between keyframes; 0 turns it off |
| `min_disparity` | `40.0` | Px at the frame's resolution; scale with preprocess (14 at 688 wide); 0 turns it off |
| `max_output_size` | `6237184` | Largest frame sent back, bytes |

## `swiftmap`

Sends each WebP frame, with its GPS (NaN if none), to the server over TCP on its own thread, and forwards the input unchanged. A plan the server sends back is the next frame's `GenericResult`; its `<LookAt>` targets and `reachback` count become the current request. `{"nfn": "hold"}` / `{"nfn": "release"}` replies set the hold; while held it polls the server every second.

| Arg | Default | |
| --- | --- | --- |
| `server` | `localhost` | SwiftMap server host |
| `server_port` | `43322` | Its TCP port |
| `max_output_size` | `1048576` | Largest result (KML), bytes |

**Wire.** `[BE uint32 size][WebP][3 × BE float64 lat, lon, alt][uint8 kind]`, kind 0 frame, 1 sent back, 2 end (no image), 3 poll. Reply: 3 native float64 (status, keyframes, total), BE uint32 length, payload.

## Delivery in NFN mode

Uriel never resends, so NFN messages are made reliable on top:

- **Status** (`swiftmap/NfnStatus`, `{"hold": bool, "request": {"id", "targets", "count"} | null}`) goes when it changes and every second. Request ids are never reused.
- **Frames sent back** are stamped (8-byte request id, 4-byte number; on the end, the count). Reachback reads each outcome from its subscription: skipped or timed out goes again, up to 3 tries; the end until it arrives. After a disconnect or 30 s without word, what is out goes again without counting a try; nothing goes while no status has come for 3 s.
- **Duplicates** are dropped: reachback starts each request once; swiftmap passes each frame and the end to the server once.

## Running by hand

```sh
STEELEAGLE_PLUGIN=swiftmap.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"server": "localhost"}' \
  STEELEAGLE_ENGINE_ENDPOINT=swiftmap pixi run start
STEELEAGLE_PLUGIN=reachback.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"min_disparity": 14}' \
  STEELEAGLE_ENGINE_ENDPOINT=reachback STEELEAGLE_REMOTE_QUALIFIER=<remote daemon IP> pixi run start
pixi run -e test test
```
