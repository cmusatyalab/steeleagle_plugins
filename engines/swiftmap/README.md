# SwiftMap

SteelEagle engine plugins for mapping with a [SwiftMap](https://github.com/cmusatyalab/swiftmap) server: `reachback`, on the vehicle, selects the keyframes to send and sends back frames already taken that cover the weak regions the server finds; `swiftmap`, on the remote daemon, sends them all to the server.

## Flow

```
vehicle daemon:  camera → JPEG encoder → reachback ══► remote daemon:  swiftmap → SwiftMap server
                                            ↑                              │
                                            └── weak targets of a plan ────┘
                                                (reachback subscribes to swiftmap)
```

With NFN mode on in the server's viewer, the server says to hold as soon as a batch fills, and `reachback` stops forwarding keyframes. After reconstructing the batch, the server plans: viewpoints over the regions VGGT reconstructed with low confidence, and how many frames to send back for them. `reachback` sends the stored frames that cover those targets, in the flow; the server maps the old images again with them, for as many rounds as set, then says to release, and keyframes resume. With NFN mode off, the server plans nothing and `reachback` only selects keyframes.

## `reachback`

Keyframe selection and reach back, on the vehicle's daemon, right after the JPEG encoder. It subscribes to the `swiftmap` engine on the remote daemon.

**Keyframes.** A JPEG `EncodedFrame` is forwarded unchanged if it passes two checks, and dropped otherwise, so it never crosses the network:

1. **Distance.** The frame was taken at least `send_distance` meters from the last frame forwarded. A frame without a GPS fix fails, unless `send_distance` is 0, which turns this check off.
2. **Keyframe.** The frame is a keyframe by the SwiftMap server's own optical-flow test, ported from `swiftmap/server/transport/keyframe_selector`: the features of the last keyframe, tracked into the frame with Lucas-Kanade optical flow, moved more than `min_disparity` pixels on average (the server's mean of |dx| and |dy| together). A `min_disparity` of 0 turns this check off.

The distance is checked first, so the last keyframe is always the last frame forwarded. Each producer (camera) is tracked on its own, and starts over with each new session or resolution. Every 5 s it logs how many frames it kept, and how many it dropped for each reason.

**The store.** It keeps one frame every 0.2 s per camera, and every keyframe it forwards, with the camera's pose when it was taken: its GPS fix and the gimbal's absolute yaw and pitch (`gimbal_info.pose_neu`). A frame without both is not stored. The newest 6000 frames are kept in a SQLite database, `swiftmap-reachback-<service>.sqlite3` in the temporary directory, which each start of the service replaces.

**Reach back.** A request names the weak targets of a plan and how many frames to send. It picks, target by target in turn, the stored frame not yet sent (keyframes count as sent) that sees the target closest to the centre of its view, within 25° of the camera's axis and 50 m of the camera, until it has that many or none is left. Then each frame that comes in is only stored, and its place in the flow carries the next frame picked, as a `swiftmap/ReachbackFrame` (the stored `EncodedFrame`), then a `swiftmap/ReachbackDone`.

**Hold.** A request can instead say to hold or release (`{"hold": true}` / `{"hold": false}`). While holding, frames are only stored and none is forwarded, besides those sent back.

| Arg | Default | Description |
| --- | --- | --- |
| `remote` | `swiftmap` | Service name of the `swiftmap` engine on the remote daemon |
| `send_distance` | `5.0` | Meters of travel between keyframes forwarded; 0 turns the distance check off |
| `min_disparity` | `40.0` | Disparity from the last keyframe, in pixels at the frame's resolution, that makes a frame the next keyframe; 0 turns the keyframe check off |
| `max_output_size` | `6237184` | Largest frame sent back, in bytes |

## `swiftmap`

Forwards each input unchanged, and sends each frame, with the GPS fix it was taken at, to a SwiftMap mapping server over TCP, as it arrives. A `RawFrame` is encoded as a JPEG first; a JPEG `EncodedFrame` is sent as it is. H.264 frames are not sent. A frame without a GPS fix is sent with a NaN one. If the server is unreachable the frame is not mapped, and the engine reconnects on the next frame. Frames are sent on a thread of their own, so the engine keeps taking frames while the server works. A `swiftmap/ReachbackFrame` goes to the server marked as a frame sent back, and a `swiftmap/ReachbackDone` as the marker that all are in.

When the server plans a next flight, the KML it sends back is the result of the next frame, as it is. Every other result is empty. The engine's result type is `GenericResult`: the vehicle stores each non-empty result in a `ComputeResult` whose `generic_result.payload` is the KML, and missions read it with `GetGenericResult`.

Each viewpoint pin in the KML holds, in a `<LookAt>`, the weakly reconstructed target it looks at, and the document's `reachback` data how many frames to send back. For each plan with targets and a count, the engine asks `reachback` for them with a derived result of type `swiftmap/ReachbackRequest`: the JSON `{"targets": [[lat, lon, alt], ...], "count": n}`.

A reply can instead carry an NFN notice: `{"nfn": "hold"}`, when the server closes a batch in NFN mode, or `{"nfn": "release"}`, once its rounds are done. The engine passes each on to `reachback` as `{"hold": true}` or `{"hold": false}`, and does not report it as a result. While held, no new frames reach the engine, so it polls the server every second for its plans.

| Arg | Default | Description |
| --- | --- | --- |
| `server` | `localhost` | Hostname or IP of the SwiftMap mapping server |
| `server_port` | `43322` | Its TCP port |
| `jpeg_quality` | `95` | JPEG quality `RawFrame`s are encoded at |
| `max_output_size` | `1048576` | Largest result sent, in bytes |

### Wire protocol

The engine sends `[uint32 size][JPEG][3 × float64 lat, lon, alt][uint8 kind]`, big-endian. The kind is 0 for a new frame, 1 for a frame sent back, and, with no JPEG, 2 once all frames sent back are in, or 3 to poll for what the server has queued; queued payloads come back one per reply, in order. The server replies with 3 native float64 (status, keyframe count, total frames; status 1 is a keyframe), then a big-endian uint32 length and that many bytes of KML (0 for an ordinary ack).

## Development

```
pixi run -e test test   # all tests, against a stand-in SwiftMap server
```
