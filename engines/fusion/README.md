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

An input still waiting when `timeout` ends, counted from when it reaches the fusion engine, is decided with what there is: the local result, or no result. This covers the cells that wait above, and an input that is never offered to an engine.

| Arg | Default | Description |
| --- | --- | --- |
| `local` | | Engine on this daemon, qualified with `STEELEAGLE_LOCAL_QUALIFIER` |
| `remote` | | Engine on the remote daemon, qualified with `STEELEAGLE_REMOTE_QUALIFIER` |
| `grace` | `200` | How long to wait for the remote result after the local one, in milliseconds |
| `timeout` | `1000` | The longest to wait on an input, in milliseconds; must be positive |

The local and remote engines must get the same inputs as the fusion engine (e.g. all three on edges from the same producer); the fusion engine waits on an input until it hears from them or it times out, and Uriel reports every offer it makes (a result, `SKIPPED`, `TIMED_OUT` or `DISCONNECTED`), but an input that is never offered to one of them holds up the engine until `timeout` ends.

Each decided input is logged at `INFO` on one line: the result chosen (`local`, `remote` or `none`) and why, then each engine's detections as `class (confidence)`, or why it has none (`no result` when it dropped the input without one, as detection-qnn does when it detects nothing; `skipped`, `timed out`, `disconnected`, `events lost` or `pending`). When both engines returned results, the remote column shows when its result arrived relative to local's (`+46 ms` after, `-12 ms` before):

```
input 412     chose: remote (in time)               | local: airplane (0.58)                   | remote +46 ms: robotic dog (0.79)
input 415     chose: remote (local had no result)   | local: no result                         | remote: robotic dog (0.73)
```

Every 30 s of decided inputs is summarized: what was chosen, what each engine did, the classes each engine reported (in how many inputs), and for the most common classes in how many inputs both engines, only remote, or only local reported them. A result that arrives after its input was decided is logged as too late. Inputs that never reach the fusion engine are not logged.

## `cop_tracking`

COP (correctable operation): reports the local engine's detections while they agree with the remote engine's on one class, and the remote engine's while they do not. It is always in one of three modes:

| Mode | Reports | Goes to |
| --- | --- | --- |
| **local-checked** | the local result, as soon as it arrives | **remote-only** when a check disagrees; **local-only** when remote is unreachable |
| **remote-only** | the remote result, as soon as it arrives, if it arrives within the grace period after the local one; local if the grace period ends first | **local-checked** when a check agrees; **local-only** when remote is unreachable |
| **local-only** | the local result, as soon as it arrives | **local-checked** when a remote result arrives in time |

It starts in local-only, until it hears from the remote engine.

A **check** runs for each input both engines answered, with the remote answer no later than `grace` after the local one, whether it was decided yet or not (in local-checked the remote answer usually arrives after the local one was reported) and even if the input never reached the COP engine. It compares the highest-confidence detection of `class` in each (case-insensitive): they agree if the intersection over union of their boxes is at least `overlap_threshold`, and disagree if it is lower or only one of them found the class. If neither did, the check is inconclusive and the mode stays. A local engine that drops an input with no result found nothing. A check on an input older than the one the mode last switched on does not switch it.

The remote engine is **unreachable** when it disconnects, times out on an input, answers more than `grace` after the local answer, or (in remote-only) has not answered when the grace period ends. A remote engine that skips an input because it was busy is not unreachable; in remote-only that input is dropped with no result, since the local one is not trusted. If the remote engine's events are lost, the inputs it had not finished fall back to the local result, but the mode does not change.

If the local engine fails an input (skipped, timed out, disconnected), the remote result is reported if there is one; an input still waiting at `timeout` is reported with what there is.

| Arg | Default | Description |
| --- | --- | --- |
| `local` | | Engine on this daemon, qualified with `STEELEAGLE_LOCAL_QUALIFIER` |
| `remote` | | Engine on the remote daemon, qualified with `STEELEAGLE_REMOTE_QUALIFIER` |
| `class` | | The class checked between local and remote; required |
| `overlap_threshold` | `0.5` | The least IoU for the boxes to agree, in (0, 1] |
| `grace` | `200` | How long after the local result the remote one is in time, in milliseconds |
| `timeout` | `1000` | The longest to wait on an input, in milliseconds; must be positive |

Each decided input is logged at `INFO` with the mode it was decided in, as for `fusion_detection`; each mode switch is logged at `INFO` with the input and reason (e.g. `input 212: local-checked -> remote-only: local and remote disagree, IoU 0.12 < 0.50`), and each check at `DEBUG`. Every 30 s, the choices, inputs per mode, check verdicts and switches are summarized.

## Development

```
pixi run -e test test   # all tests, including one through a real Uriel server
```
