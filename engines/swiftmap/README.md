# SwiftMap

SteelEagle engine plugins for mapping with a [SwiftMap](https://github.com/cmusatyalab/swiftmap) server: `swiftmap` sends frames to the server in the order they were taken, `keyframe` discards the frames the server would not keep before they are sent, and `reachback` sends back the frames lost on the way.

## Flow

```
vehicle daemon:  camera → keyframe → JPEG encoder → reachback ──→ remote daemon:  swiftmap → SwiftMap server
                                                        ↑                              │
                                                        └── requests for lost frames ──┘
                                                            (reachback subscribes to swiftmap)
```

The JPEG encoder must come right after `keyframe`: it numbers the frames it encodes, one after another, in `EncodedFrame.id`, so a missing number at `swiftmap` is a frame lost on the way. Lost frames go back outside the flow, as files on the remote daemon.

## `swiftmap`

Forwards each camera frame (`RawFrame`, or `EncodedFrame` in JPEG) unchanged, and sends it with the GPS fix it was taken at to a SwiftMap mapping server over TCP. A `RawFrame` is encoded as a JPEG first; a JPEG `EncodedFrame` is sent as it is. H.264 frames are not sent. A frame without a GPS fix is sent with a NaN one. Choosing which frames to send is the job of the `keyframe` engine. If the server is unreachable the frame is not mapped, and the engine reconnects on the next frame.

Frames go to the server in the order they were taken. A frame that arrives in order is sent at once. When frame numbers jump (1, 2, 3, then 6), the engine:

1. holds the frames after the gap (6 and later);
2. asks the `reachback` engine for the missing frames (4 to 5), with a derived result of type `swiftmap/ReachbackRequest`, the JSON `{"first": 4, "last": 5}`, and asks again every 10 s while any is missing;
3. takes each one in as it arrives, as `reachback-<number>.frame` in the daemon's files directory (`STEELEAGLE_FILES`), and removes the file;
4. sends the missing frames and the frames held, in order, once all are back. A frame still missing `gap_timeout` seconds after the gap was found is given up on, and the frames after it are sent without it.

Numbers that start over (the JPEG encoder restarted) or a new camera session send the frames held, and start over. Over 300 frames held gives up on the missing ones. Frames are sent on a thread of their own, so the engine keeps taking frames while the server works. A `gap_timeout` of 0 sends each frame as it arrives instead, in any order, and asks for nothing; use it when frames are not numbered one after another, e.g. `RawFrame`s straight from `keyframe`.

When the server plans a next-flight area, the KML it sends back is the result of the next frame, as it is. Every other result is empty. The engine's result type is `GenericResult`: the vehicle stores each non-empty result in a `ComputeResult` whose `generic_result.payload` is the KML, and missions read it with `GetGenericResult`.

| Arg | Default | Description |
| --- | --- | --- |
| `server` | `localhost` | Hostname or IP of the SwiftMap mapping server |
| `server_port` | `43322` | Its TCP port |
| `jpeg_quality` | `95` | JPEG quality `RawFrame`s are encoded at |
| `gap_timeout` | `30.0` | Seconds to wait for lost frames before sending the frames after them without them; 0 sends each frame as it arrives |
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

Run it on the daemon whose camera takes the frames, between the camera and the `swiftmap` engine (or a JPEG encoder in front of it). Since it already selects keyframes, run the server with `SWIFTMAP_MIN_DISPARITY=0` (or the keyframe filter off in its viewer), so it keeps every frame it gets. Every 5 s it logs how many frames it kept, and how many it dropped for each reason (no GPS fix, within `send_distance`, not a keyframe).

| Arg | Default | Description |
| --- | --- | --- |
| `send_distance` | `5.0` | Meters of travel between frames forwarded; 0 turns the distance check off |
| `min_disparity` | `40.0` | Disparity from the last keyframe, in pixels at the frame's resolution, that makes a frame the next keyframe; 0 turns the keyframe check off |

## `reachback`

Stores every `EncodedFrame` it gets and forwards it on unchanged. It runs on the vehicle's daemon, right after the JPEG encoder, and subscribes to the `swiftmap` engine on the remote daemon. For each request, it sends the stored frames numbered in the range, of the producer and session the request is about, to the remote daemon as files named `reachback-<number>.frame`, each the `EncodedFrame` as it was stored. A frame not stored is not sent; `swiftmap` gives up on it after `gap_timeout`.

The files go outside the flow: through the local daemon's `TransferService` (`STEELEAGLE_CLIENT_SOCKET`), routed by the remote daemon's IP (`STEELEAGLE_REMOTE_QUALIFIER`), in 1 MiB chunks, one file at a time. The remote daemon saves them in its files directory. A transfer that fails is logged, and the request repeated by `swiftmap` sends it again. The newest 1000 frames are kept in a SQLite database, `swiftmap-reachback-<service>.sqlite3` in the temporary directory, which each start of the service replaces; `swiftmap` never asks for older ones.

| Arg | Default | Description |
| --- | --- | --- |
| `remote` | `swiftmap` | Service name of the `swiftmap` engine on the remote daemon |

## Development

```
pixi run -e test test   # all tests, against a stand-in SwiftMap server and daemon
```
