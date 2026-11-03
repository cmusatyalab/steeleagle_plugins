# Fusion

SteelEagle engine plugins that fuse the results of an engine on this daemon and an engine on the remote daemon. They take `RawFrame`s or `EncodedFrame`s, and drop every frame: the fused result is the frame's result, and nothing is forwarded. The engine never reads the frame; it subscribes to the events of both engines and decides each frame from what they did with the same input.

## `fusion_detection`

Reports the remote engine's `DetectionResult` if it arrives within the grace period after the local one, and the local one otherwise.

For each input, each engine has either:

- a **result**: a `RESULT` event, unless the engine dropped the input with no result. A forwarded empty `DetectionResult` (no detections) is a result.
- a **failure**: a `RESULT` event that dropped the input with no result, or a `SKIPPED` or `TIMED_OUT` event. A `DISCONNECTED` event fails every input the engine has not finished, and every input that arrives until the engine is heard from again. A `LOST` event fails every input the engine has not finished, since their results may have been lost; a result that still arrives before the input is decided is used.

| remote \ local | result | failure | pending |
| --- | --- | --- | --- |
| **result** | remote if it arrived by the end of the grace period, else local | remote | remote |
| **failure** | local, at once | dropped, no result | wait |
| **pending** | wait; local when the grace period ends | wait | wait |

The grace period starts when the fusion engine receives the local result, which may be before the input reaches it.

| Arg | Default | Description |
| --- | --- | --- |
| `local` | | Engine on this daemon, qualified with `STEELEAGLE_LOCAL_QUALIFIER` |
| `remote` | | Engine on the remote daemon, qualified with `STEELEAGLE_REMOTE_QUALIFIER` |
| `grace` | `200` | How long to wait for the remote result after the local one, in milliseconds |

The local and remote engines must get the same inputs as the fusion engine (e.g. all three on edges from the same producer); the fusion engine waits on an input until it hears from them, and Uriel reports every offer it makes (a result, `SKIPPED`, `TIMED_OUT` or `DISCONNECTED`), but an input that is never offered to one of them is waited on forever.

Each decided input is logged at `INFO`: which engine accepted first (returned a result), when the other accepted, and which result was returned and why, or that neither accepted. A result that arrives after its input was decided is logged as too late. Inputs that never reach the fusion engine are not logged.

## Development

```
pixi run -e test test   # all tests, including one through a real Uriel server
```
