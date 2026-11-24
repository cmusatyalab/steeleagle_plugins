# SwiftMap

SteelEagle plugins for mapping with a [SwiftMap](https://github.com/cmusatyalab/swiftmap) server.

```
vehicle:  camera → preprocess → reachback (stores, dead end)
                                 ├─ producer keyframes (1 token) ══╗
                                 └─ producer sendback  (1 token) ══╩══► remote:  swiftmap → SwiftMap server
                                   ↑                                                │
                                   └──────── NFN status (reachback subscribes) ─────┘
```

| Plugin | Runs on | Code | Does |
| --- | --- | --- | --- |
| `preprocess` | vehicle | `onboard.py` | Shrinks each frame to the VGGT-Omega model input and sends lossless WebP |
| `reachback` | vehicle | `reachback.py`, `onboard.py` | Stores every frame; publishes keyframes, and stored frames covering a plan's weak targets |
| `swiftmap` | remote | `remote.py` | Sends frames to the server; turns its plans into reachback requests |

Pacing is Uriel's: each of reachback's producers has one token, so a keyframe or
frame sent back goes only when the link has taken the last one, and a newer
keyframe replaces one still waiting.

`protocol.py` holds what both sides share; `main.py` starts the plugin eagled names. Each arg's default lives only in its `.plugin.toml`.

## `preprocess`

Does VGGT-Omega's preprocessing on board: center-crop an aspect ratio outside [0.5, 2], then PIL-bicubic resize to the `balanced` shape, whole `patch_size` patches about (`image_resolution`/`patch_size`)² in all (688×384 for 16:9 at 512). Sent lossless, the server's own preprocessing changes nothing, so the model input is exactly what it would make from the full frame, at about a quarter of the bytes. `image_resolution` and `patch_size` must match the server's `VGGT_OMEGA_IMAGE_RESOLUTION`. WebP goes as `CODEC_UNSPECIFIED` (the protocol has no WebP codec yet) and is recognised by its RIFF header.

| Arg | Default | |
| --- | --- | --- |
| `image_resolution` | `512` | The server's `VGGT_OMEGA_IMAGE_RESOLUTION` |
| `patch_size` | `16` | Model patch size, px |
| `lossless` | `true` | `false` for lossy WebP at `quality`; lossless uses the least encoder effort (~5 ms a 688×384 frame, ~8% more bytes than the default effort's ~90 ms) |
| `quality` | `90` | WebP quality when not lossless |
| `max_output_size` | `6237184` | Largest frame sent, bytes |

## `reachback`

A dead end: each camera frame is stored and dropped, so the camera path never waits on the network.

**Keyframes.** A frame is published on the `keyframes` producer if it was taken at least `send_distance` m from the last keyframe (no GPS fix fails, unless 0) and is a keyframe by the server's optical-flow test: the last keyframe's features moved more than `min_disparity` px on average. None is published while the NFN status says hold, or while frames are being sent back.

**Store.** One frame every 0.2 s, plus every keyframe, with its GPS and gimbal yaw/pitch, in SQLite (newest 6000).

**Reach back.** For each new request in the NFN status, it picks one frame per target, for up to `count` targets: the unsent stored frame seeing it nearest the view's centre, within 25° and 50 m. A target no frame covers gets none. It publishes them on the `sendback` producer one at a time, each once the last one's turn in the flow has ended (whatever its outcome), then an `EncodedFrame` with no image as the end marker. Nothing is sent again.

The flow names the producers `local.reachback.keyframes` and `local.reachback.sendback`, each with an edge to the `swiftmap` engine and `tokens = 1`.

| Arg | Default | |
| --- | --- | --- |
| `remote` | `swiftmap` | Service name of the `swiftmap` engine |
| `send_distance` | `5.0` | Meters between keyframes; 0 turns it off |
| `min_disparity` | `40.0` | Px at the frame's resolution; scale with preprocess (14 at 688 wide); 0 turns it off |
| `max_output_size` | `6237184` | Largest frame published, bytes |

## `swiftmap`

Sends each WebP frame, with its GPS (NaN if none), to the server over TCP on its own thread: a keyframe as a new frame; from the `sendback` producer, a frame sent back, or the end marker. A plan the server sends back is the next frame's `GenericResult`; its `<LookAt>` targets and `reachback` count become the current request. `{"nfn": "hold"}` / `{"nfn": "release"}` replies set the hold. It polls the server after a second idle, so it hears of holds and plans without frames coming in.

**NFN status.** A derived result of type `swiftmap/NfnStatus`, `{"hold": bool, "request": {"targets": [[lat, lon, alt], ...], "count": n} | null}`, published once each time it changes; reachback subscribes to it.

| Arg | Default | |
| --- | --- | --- |
| `server` | `localhost` | SwiftMap server host |
| `server_port` | `43322` | Its TCP port |
| `max_output_size` | `1048576` | Largest result (KML), bytes |

**Wire.** `[BE uint32 size][WebP][3 × BE float64 lat, lon, alt][uint8 kind]`, kind 0 frame, 1 sent back, 2 end (no image), 3 poll. Reply: 3 native float64 (status, keyframes, total), BE uint32 length, payload.

## Running by hand

```sh
STEELEAGLE_PLUGIN=swiftmap.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"server": "localhost"}' \
  STEELEAGLE_ENGINE_ENDPOINT=swiftmap pixi run start
STEELEAGLE_PLUGIN=reachback.plugin.toml STEELEAGLE_PLUGIN_ARGS='{"min_disparity": 14}' \
  STEELEAGLE_ENGINE_ENDPOINT=reachback STEELEAGLE_REMOTE_QUALIFIER=<remote daemon IP> pixi run start
  # publishes on reachback.keyframes and reachback.sendback
pixi run -e test test
```
