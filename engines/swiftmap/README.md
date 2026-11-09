# SwiftMap

SteelEagle engine plugins for mapping with a [SwiftMap](https://github.com/cmusatyalab/swiftmap) server.

## `swiftmap`

Forwards each camera frame (`RawFrame`, or `EncodedFrame` in JPEG) unchanged, and sends it with the GPS fix it was taken at to a SwiftMap mapping server over TCP. A `RawFrame` is encoded as a JPEG first; a JPEG `EncodedFrame` is sent as it is. H.264 frames are not sent.

At most one frame is sent per `send_distance` meters of travel since the last frame the server got. Frames without a GPS fix are not sent, unless `send_distance` is 0. If the server is unreachable the frame is not mapped, and the engine reconnects on the next frame.

When the server plans a next-flight area, the KML it sends back is that frame's result, as it is. Every other result is empty. The engine's result type is `GenericResult`: the vehicle stores each non-empty result in a `ComputeResult` whose `generic_result.payload` is the KML, and missions read it with `GetGenericResult`.

| Arg | Default | Description |
| --- | --- | --- |
| `server` | `localhost` | Hostname or IP of the SwiftMap mapping server |
| `server_port` | `43322` | Its TCP port |
| `send_distance` | `5.0` | Meters of travel between frames sent; 0 sends every frame |
| `jpeg_quality` | `95` | JPEG quality `RawFrame`s are encoded at |
| `max_output_size` | `1048576` | Largest result sent, in bytes |

### Wire protocol

The engine sends `[uint32 size][JPEG][3 × float64 lat, lon, alt]`, big-endian. The server replies with 3 native float64 (status, keyframe count, total frames; status 1 is a keyframe), then a big-endian uint32 length and that many bytes of KML (0 for an ordinary ack).

## Development

```
pixi run -e test test   # all tests, against a stand-in SwiftMap server
```
