# Storage

SteelEagle engine plugins that store encoded camera frames (`EncodedFrame`) in a SQLite database and send stored frames to an engine on the remote daemon later. Each frame is stored under its Uriel input id (producer, session, sequence number), indexed by the vehicle's pose when it was captured: latitude, longitude, global altitude, global heading, relative x, y, z, and relative heading. A field the frame does not set is NaN, which SQLite stores as NULL. Every frame is dropped; stored frames go on in place of inputs, so they are sent at the rate inputs arrive.

A stored frame goes out by replacing an input, and keeps that input's id. The remote engine's events therefore name the input a frame went out in place of, not the frame's own sequence number.

## `priority_reintegration`

Sends the remote engine, once it is reachable again, the frames the local classifier found the target class in while it was not, highest score first.

| Mode | Entered on | Frames |
| --- | --- | --- |
| idle | start | dropped, not stored |
| storing | `DISCONNECTED` from the remote engine | stored and dropped |
| reintegrating | `SENT`, `ACCEPTED` or `RESULT` from the remote engine, while storing | not stored; a queued frame replaces each input |

- A classifier `RESULT` event whose `ClassificationResult` has the target class queues that frame, with the score as its priority.
- While reintegrating, one frame is in flight at a time. Inputs are dropped until the remote engine's `SENT` event for the input the frame went out as. A `SKIPPED` or `TIMED_OUT` event for that input, or a `LOST` event, sends the same frame again in place of the next input.
- A `DISCONNECTED` event returns to storing and puts the unconfirmed frame back in the queue.

| Arg | Default | Description |
| --- | --- | --- |
| `remote` | | Engine on the remote daemon, qualified with `STEELEAGLE_REMOTE_QUALIFIER` |
| `classifier` | | Local classifier engine, qualified with `STEELEAGLE_LOCAL_QUALIFIER` |
| `class` | | Target class, ignoring case |
| `database` | `""` | SQLite file, kept across restarts; empty for a temporary one |
| `max_output_size` | `6237184` | Largest frame stored and sent, in bytes |

## `reachback`

Stores every frame. The remote engine asks for one with a derived result (`Engine.publish_derived`) holding a `storage.v1.ReachbackRequest` (`src/storage/proto/storage.proto`), with result type `storage/v1/ReachbackRequest`:

- If `sequence_number` is set, that frame is picked.
- Otherwise, the frame with the lowest mean squared error over the fields that are set is picked. A frame without one of those fields is not a candidate. Heading differences wrap around at 360°. The fields are compared in their own units (degrees, meters), unweighted.

The picked frame replaces the next input. Requests are answered in order, one per input.

| Arg | Default | Description |
| --- | --- | --- |
| `remote` | | Engine on the remote daemon, qualified with `STEELEAGLE_REMOTE_QUALIFIER` |
| `database` | `""` | SQLite file, kept across restarts; empty for a temporary one |
| `max_output_size` | `6237184` | Largest frame stored and sent, in bytes |

## Development

```
pixi run -e test test          # all tests, including one through a real Uriel server
pixi run -e generate generate  # regenerates src/storage/storage_pb2.py
```
