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

Every NFN message is sent again until it arrives, since the link between the vehicle and the remote daemon may lose it; see [Delivery in NFN mode](#delivery-in-nfn-mode).

## `reachback`

Keyframe selection and reach back, on the vehicle's daemon, right after the JPEG encoder. It subscribes to the `swiftmap` engine on the remote daemon.

**Keyframes.** A JPEG `EncodedFrame` is forwarded unchanged if it passes two checks, and dropped otherwise, so it never crosses the network:

1. **Distance.** The frame was taken at least `send_distance` meters from the last frame forwarded. A frame without a GPS fix fails, unless `send_distance` is 0, which turns this check off.
2. **Keyframe.** The frame is a keyframe by the SwiftMap server's own optical-flow test, ported from `swiftmap/server/transport/keyframe_selector`: the features of the last keyframe, tracked into the frame with Lucas-Kanade optical flow, moved more than `min_disparity` pixels on average (the server's mean of |dx| and |dy| together). A `min_disparity` of 0 turns this check off.

The distance is checked first, so the last keyframe is always the last frame forwarded. Each producer (camera) is tracked on its own, and starts over with each new session or resolution. Every 5 s it logs how many frames it kept, and how many it dropped for each reason.

**The store.** It keeps one frame every 0.2 s per camera, and every keyframe it forwards, with the camera's pose when it was taken: its GPS fix and the gimbal's absolute yaw and pitch (`gimbal_info.pose_neu`). A frame without both is not stored. The newest 6000 frames are kept in a SQLite database, `swiftmap-reachback-<service>.sqlite3` in the temporary directory, which each start of the service replaces.

**Reach back.** The `swiftmap` engine's NFN status names the current request: the weak targets of a plan and how many frames to send. For a request it has not seen, it picks, target by target in turn, the stored frame not yet sent (keyframes count as sent) that sees the target closest to the centre of its view, within 25° of the camera's axis and 50 m of the camera, until it has that many or none is left. Then each frame that comes in is only stored, and its place in the flow carries the next frame picked, as a `swiftmap/ReachbackFrame` (the stored `EncodedFrame`, stamped with the request), then a `swiftmap/ReachbackDone`. A frame counts as sent once it arrives; any that does not is sent again (see [Delivery in NFN mode](#delivery-in-nfn-mode)).

**Hold.** The status also says whether to hold. While holding, frames are only stored and none is forwarded, besides those sent back.

| Arg | Default | Description |
| --- | --- | --- |
| `remote` | `swiftmap` | Service name of the `swiftmap` engine on the remote daemon |
| `send_distance` | `5.0` | Meters of travel between keyframes forwarded; 0 turns the distance check off |
| `min_disparity` | `40.0` | Disparity from the last keyframe, in pixels at the frame's resolution, that makes a frame the next keyframe; 0 turns the keyframe check off |
| `max_output_size` | `6237184` | Largest frame sent back, in bytes |

## `swiftmap`

Forwards each input unchanged, and sends each frame, with the GPS fix it was taken at, to a SwiftMap mapping server over TCP, as it arrives. A `RawFrame` is encoded as a JPEG first; a JPEG `EncodedFrame` is sent as it is. H.264 frames are not sent. A frame without a GPS fix is sent with a NaN one. If the server is unreachable the frame is not mapped, and the engine reconnects on the next frame. Frames are sent on a thread of their own, so the engine keeps taking frames while the server works. A `swiftmap/ReachbackFrame` for the current request goes to the server marked as a frame sent back, and its `swiftmap/ReachbackDone` as the marker that all are in, each once.

When the server plans a next flight, the KML it sends back is the result of the next frame, as it is. Every other result is empty. The engine's result type is `GenericResult`: the vehicle stores each non-empty result in a `ComputeResult` whose `generic_result.payload` is the KML, and missions read it with `GetGenericResult`.

Each viewpoint pin in the KML holds, in a `<LookAt>`, the weakly reconstructed target it looks at, and the document's `reachback` data how many frames to send back. A plan with targets and a count is the current request, which the engine tells `reachback` in its NFN status.

A reply can instead carry an NFN notice: `{"nfn": "hold"}`, when the server closes a batch in NFN mode, or `{"nfn": "release"}`, once its rounds are done. Either ends the current request. The engine tells `reachback` in its NFN status whether the server holds, and does not report the notice as a result. While held, no new frames reach the engine, so it polls the server every second for its plans.

| Arg | Default | Description |
| --- | --- | --- |
| `server` | `localhost` | Hostname or IP of the SwiftMap mapping server |
| `server_port` | `43322` | Its TCP port |
| `jpeg_quality` | `95` | JPEG quality `RawFrame`s are encoded at |
| `max_output_size` | `1048576` | Largest result sent, in bytes |

### Wire protocol

The engine sends `[uint32 size][JPEG][3 × float64 lat, lon, alt][uint8 kind]`, big-endian. The kind is 0 for a new frame, 1 for a frame sent back, and, with no JPEG, 2 once all frames sent back are in, or 3 to poll for what the server has queued; queued payloads come back one per reply, in order. The server replies with 3 native float64 (status, keyframe count, total frames; status 1 is a keyframe), then a big-endian uint32 length and that many bytes of KML (0 for an ordinary ack).

## Delivery in NFN mode

The vehicle and the remote daemon talk over Uriel, which reconnects on its own but never sends a lost message again: an input that does not reach the engine in time ends skipped or timed out, and an event published while the link is down is gone. Losing a keyframe is fine, since another follows; losing an NFN message is not, so in NFN mode each is sent until it arrives. The `swiftmap` engine and the server run side by side, and their TCP link is taken not to lose messages.

- **Status, `swiftmap` → `reachback`.** A derived result of type `swiftmap/NfnStatus`, `{"hold": true, "request": {"id": 7, "targets": [[lat, lon, alt], ...], "count": n}}` (`request` is null when there is none), at once when it changes and every second from the server's first NFN notice on, so one lost is made good a second later. Request ids are never reused, across restarts of the engine too.
- **Frames sent back and their end, `reachback` → `swiftmap`.** Each is stamped with the request id and a number: 8 bytes of id and 4 of number, big-endian, then the stored `EncodedFrame`; on the end, the number is how many frames there are. `reachback` reads what became of each from its subscription to `swiftmap`, whose result means it arrived. A frame skipped or timed out goes again, up to 3 times; the end goes once no frame is out, and again until it arrives. After a lost connection, or no word for 30 s, what is out goes again without counting as a try, and nothing goes while no status has come for 3 s.
- **Duplicates.** `reachback` starts a request once, however many statuses name it, and drops what is left of it when a status names none. `swiftmap` passes each frame and the end on to the server once, and drops anything for another request.

If the end never arrives, the server stops waiting after `NFN_REACHBACK_TIMEOUT_S` (120 s by default), maps with the frames that came, and releases; `reachback` follows the next status.

Give the flow's `reachback` → `swiftmap` edge a timeout long enough for a frame to cross the link, several seconds on a slow one: a frame that takes longer counts as a failed try.

## Development

```
pixi run -e test test   # all tests, against a stand-in SwiftMap server
```
