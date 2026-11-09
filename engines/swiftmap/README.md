# SwiftMap

SteelEagle engine plugins for mapping with a [SwiftMap](https://github.com/cmusatyalab/swiftmap) server: `swiftmap` sends frames to the server, and `keyframe` discards the frames the server would not keep before they are sent.

## `swiftmap`

Forwards each camera frame (`RawFrame`, or `EncodedFrame` in JPEG) unchanged, and sends it with the GPS fix it was taken at to a SwiftMap mapping server over TCP. A `RawFrame` is encoded as a JPEG first; a JPEG `EncodedFrame` is sent as it is. H.264 frames are not sent.

Every frame the engine gets is sent; a frame without a GPS fix is sent with a NaN one. Choosing which frames to send is the job of the `keyframe` engine in front of it. If the server is unreachable the frame is not mapped, and the engine reconnects on the next frame.

When the server plans a next-flight area, the KML it sends back is that frame's result, as it is. Every other result is empty. The engine's result type is `GenericResult`: the vehicle stores each non-empty result in a `ComputeResult` whose `generic_result.payload` is the KML, and missions read it with `GetGenericResult`.

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

Run it on the daemon whose camera takes the frames, between the camera and the `swiftmap` engine (or a JPEG encoder in front of it). Since it already selects keyframes, run the server with `SWIFTMAP_MIN_DISPARITY=0` (or the keyframe filter off in its viewer), so it keeps every frame it gets. Every 5 s it logs how many frames it kept, and how many it dropped for each reason (no GPS fix, within `send_distance`, not a keyframe).

| Arg | Default | Description |
| --- | --- | --- |
| `send_distance` | `5.0` | Meters of travel between frames forwarded; 0 turns the distance check off |
| `min_disparity` | `40.0` | Disparity from the last keyframe, in pixels at the frame's resolution, that makes a frame the next keyframe; 0 turns the keyframe check off |

## Development

```
pixi run -e test test   # all tests, against a stand-in SwiftMap server
```
