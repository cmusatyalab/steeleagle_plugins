# SwiftMap

SteelEagle engine plugins for mapping with a [SwiftMap](https://github.com/cmusatyalab/swiftmap) server: `swiftmap` sends frames to the server, `keyframe` discards the frames the server would not keep before they are sent, and `reachback` sends back the frames already taken that best cover the weak regions the server finds.

## Flow

```
vehicle daemon:  camera → JPEG encoder → reachback → keyframe ──→ remote daemon:  swiftmap → SwiftMap server
                                            ↑                                         │
                                            └──── viewpoints of the next-flight plan ─┘
                                                  (reachback subscribes to swiftmap)
```

`reachback` comes before `keyframe`, so it stores frames `keyframe` drops. After each batch, the server plans viewpoints over the weakly reconstructed regions (only those of the focus, if one is set in its viewer). For each viewpoint, `reachback` sends the stored frame that looks at its target from the closest angle, outside the flow, as a file on the remote daemon.

## `swiftmap`

Forwards each camera frame (`RawFrame`, or `EncodedFrame` in JPEG) unchanged, and sends it with the GPS fix it was taken at to a SwiftMap mapping server over TCP, as it arrives. A `RawFrame` is encoded as a JPEG first; a JPEG `EncodedFrame` is sent as it is. H.264 frames are not sent. A frame without a GPS fix is sent with a NaN one. If the server is unreachable the frame is not mapped, and the engine reconnects on the next frame. Frames are sent on a thread of their own, so the engine keeps taking frames while the server works.

When the server plans a next flight, the KML it sends back is the result of the next frame, as it is. Every other result is empty. The engine's result type is `GenericResult`: the vehicle stores each non-empty result in a `ComputeResult` whose `generic_result.payload` is the KML, and missions read it with `GetGenericResult`.

Each viewpoint pin in the KML holds its position and, in a `<LookAt>`, the target it looks at. For each plan, the engine asks `reachback` for the frames closest to its viewpoints with a derived result of type `swiftmap/ReachbackRequest`: the JSON `{"viewpoints": [[lat, lon, alt, target lat, target lon, target alt], ...], "exclude": [...]}`, where `exclude` holds the numbers of the frames already sent to the server. Frames come back as `reachback-<number>.frame` in the daemon's files directory (`STEELEAGLE_FILES`); the engine sends each one it has not sent yet, in order, and removes the file.

| Arg | Default | Description |
| --- | --- | --- |
| `server` | `localhost` | Hostname or IP of the SwiftMap mapping server |
| `server_port` | `43322` | Its TCP port |
| `jpeg_quality` | `95` | JPEG quality `RawFrame`s are encoded at |
| `max_output_size` | `1048576` | Largest result sent, in bytes |

### Wire protocol

The engine sends `[uint32 size][JPEG][3 × float64 lat, lon, alt]`, big-endian. The server replies with 3 native float64 (status, keyframe count, total frames; status 1 is a keyframe), then a big-endian uint32 length and that many bytes of KML (0 for an ordinary ack).

## `keyframe`

Early discard for SwiftMap. A camera frame (`RawFrame`, or `EncodedFrame` in JPEG) is forwarded unchanged if it passes two checks, and dropped otherwise, so it never crosses the network. It has no result.

1. **Distance.** The frame was taken at least `send_distance` meters from the last frame forwarded. A frame without a GPS fix fails, unless `send_distance` is 0, which turns this check off.
2. **Keyframe.** The frame is a keyframe by the SwiftMap server's own optical-flow test (below). A `min_disparity` of 0 turns this check off.

The distance is checked first. It is cheaper, and a frame dropped for being too close never reaches the keyframe test, so the last keyframe is always the last frame forwarded. A frame that passes the distance check but is not a keyframe does not move where the distance is measured from. The distance is measured from the last frame this engine forwarded, whether or not the server got it.

The keyframe test is the SwiftMap server's own keyframe selector, ported from `swiftmap/server/transport/keyframe_selector`. The features of the last keyframe are tracked into each frame with Lucas-Kanade optical flow. The frame is the next keyframe if they moved more than `min_disparity` pixels on average. With the same threshold, it keeps the same frames as the server. A frame with too few features to track is dropped, and the next frame is tried as the keyframe instead. Each producer (camera) is tracked on its own, and starts over with each new session or resolution.

The disparity is the server's: the mean of |dx| and |dy| together, not the mean Euclidean displacement, so a feature that moves 60 px sideways counts 30. It is kept so that one threshold means the same in both places; change it in both together.

Run it on the daemon whose camera takes the frames, after `reachback` and before the `swiftmap` engine. Since it already selects keyframes, run the server with `SWIFTMAP_MIN_DISPARITY=0` (or the keyframe filter off in its viewer), so it keeps every frame it gets. Every 5 s it logs how many frames it kept, and how many it dropped for each reason (no GPS fix, within `send_distance`, not a keyframe).

| Arg | Default | Description |
| --- | --- | --- |
| `send_distance` | `5.0` | Meters of travel between frames forwarded; 0 turns the distance check off |
| `min_disparity` | `40.0` | Disparity from the last keyframe, in pixels at the frame's resolution, that makes a frame the next keyframe; 0 turns the keyframe check off |

## `reachback`

Stores frames and forwards every frame unchanged. It runs on the vehicle's daemon, after the JPEG encoder and before `keyframe`, and subscribes to the `swiftmap` engine on the remote daemon. It stores one `EncodedFrame` every 0.2 s per camera, with the camera's pose when it was taken: its GPS fix and the gimbal's absolute yaw and pitch (`gimbal_info.pose_neu`). A frame without both is not stored.

For each viewpoint of a request, it sends the stored frame, of the producer and session the request is about, that:

1. looks at the viewpoint's target, which lies within 30° of the camera's axis;
2. sees it from within 30° of the viewpoint's angle, and from at most twice its distance;
3. is not in `exclude`, and was not sent before.

Of those, it sends the one seen from the closest angle; a viewpoint none fits gets no frame. Each is sent to the remote daemon as a file named `reachback-<number>.frame`, the `EncodedFrame` as it was stored.

The files go outside the flow: through the local daemon's `TransferService` (`STEELEAGLE_CLIENT_SOCKET`), routed by the remote daemon's IP (`STEELEAGLE_REMOTE_QUALIFIER`), in 1 MiB chunks, one file at a time. The remote daemon saves them in its files directory. A transfer that fails is logged and not retried. The newest 6000 frames (20 minutes) are kept in a SQLite database, `swiftmap-reachback-<service>.sqlite3` in the temporary directory, which each start of the service replaces.

| Arg | Default | Description |
| --- | --- | --- |
| `remote` | `swiftmap` | Service name of the `swiftmap` engine on the remote daemon |

## Development

```
pixi run -e test test   # all tests, against a stand-in SwiftMap server and daemon
```
