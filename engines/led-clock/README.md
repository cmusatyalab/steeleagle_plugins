# LED Clock

A SteelEagle engine plugin that reads an LED clock off camera frames and logs latency. It takes `RawFrame`s and drops every one, with no result.

A Raspberry Pi running `ledclock.py` shows `tick = floor(CLOCK_REALTIME / 5 ms) mod 1024` as Gray code on 10 red LEDs. Two yellow markers, always on, sit at the ends of the row. Point the camera at the breadboard. For each frame, the engine:

1. Notes the time the frame was received, before parsing it.
2. Finds the two yellow markers, then samples the 10 LEDs at their breadboard rows between them. The markers are found again in every frame, since stabilization moves the image.
3. Reads each LED as on or off against its own threshold, set from the levels that LED has shown over the last 10 s. It needs about one wrap of the clock (5.12 s) before every bit can be read.
4. Converts Gray code to the tick. The display time is the middle of the latest tick at or before the receive time that has that code.
5. Computes two latencies:
   - **latency**: receive time here, less the display time.
   - **to fermi**: the frame's `timestamp` (set when the driver published it), less the display time.

Both latencies assume the Pi's clock, the driver host's clock and this host's clock agree, for example through chrony.

As a check that needs no clocks, each tick is compared with the last one read. Frames come `1/fps` apart, so ticks should step by about a whole number of frames' worth (6.67 at 30 fps), within 2 ticks.

The LED row's layout is fixed in `decode.py`, as measured on the Anafi's frames. Bit 0 is the leftmost data LED, 6 breadboard rows from the left marker. The data LEDs are 5 rows apart, and the right marker is 5 rows past bit 9. Each LED's brightness is its mean luma over a small patch. Luma works better than the brightest channel because unlit LEDs pick up red glow from their lit neighbours.

## Output

A log line every `report_interval_seconds`, and one for the whole run when the engine stops:

```
last 5 s: 150 frames, 148 decoded (99%), steps ok 147/147 | latency ms: mean 121.0 std 4.1 min 112.3 p50 120.3 p95 131.0 p99 140.2 max 151.7 | to fermi ms: ...
whole run, 600 s: 18000 frames, ...
```

A row per frame in `<files>/<engine>/frames.csv` (paths as for the snapshots below). The rows are appended, so an engine restart keeps the earlier ones. Columns:

| Column | Meaning |
| --- | --- |
| `seq` | Sequence number of the frame's input |
| `recv_ns` | When the frame was received here (realtime clock) |
| `fermi_ts_ns` | The frame's `timestamp`, set when the driver published it |
| `tick` | The clock tick read off the frame |
| `display_ns` | When the clock showed that tick: the middle of the tick |
| `latency_ms` | `recv_ns - display_ns` |
| `fermi_latency_ms` | `fermi_ts_ns - display_ns` |
| `confidence` | How clearly the least clear LED was on or off, 0 to 1 |
| `step_ok` | Whether the tick stepped by a whole number of frames from the last one read |

A frame whose clock could not be read has empty fields after `recv_ns` (and `fermi_ts_ns`). To collect the file after each trial, list it in the experiment's `[results]`.

### Statistics of a run

```
pixi run summarize path/to/frames.csv [--min-confidence 0.5] [--start 10] [--end 600]
```

This prints how many frames were used, then the mean, standard deviation, min, p50, p95, p99 and max of both latencies. `--start` and `--end` are seconds after the first frame; use `--start` to leave out the first ~5 s, before every LED has been seen on and off. The CSV also loads straight into pandas for plots.

An annotated snapshot every `snapshot_interval_seconds`, saved as `<files>/<engine>/<seq>.jpeg`, where `<files>` is the daemon's files directory (`STEELEAGLE_FILES`, or `/steeleagle/files` in a sandbox) and `<engine>` is the qualified `STEELEAGLE_ENGINE_ENDPOINT`. It shows:

- a ring on each marker;
- a ring on each LED (green for on, grey for off, magenta if not read) with the bit read under it;
- the bits left to right, the Gray code, the tick, the decoding confidence and both latencies;
- each LED's level and threshold.

| Arg | Default | Description |
| --- | --- | --- |
| `tick_ms` | `5` | Milliseconds per tick; must match `ledclock.py --tick-ms` |
| `fps` | `30` | Camera frame rate, for the step check |
| `report_interval_seconds` | `5` | Seconds between log lines |
| `snapshot_interval_seconds` | `5` | Seconds between snapshots |

## Flow

Put it after the H.264 decoder on the remote daemon:

```
anafi rgb → h264 encoder (vehicle daemon) ══ remote daemon ══► h264 decoder → led-clock
```

## Camera

- Use a fast shutter (1/500 s or less, shorter than one tick), and turn HDR off.
- Keep the LED row horizontal in the image (rolling shutter).
- Keep the LEDs at least about 10 px apart in the frame: within about 1 m of the Anafi's 720p stream.

## Tests

```
pixi run -e test test
```

The tests draw synthetic frames of the breadboard. The end-to-end test sends a frame through a real Uriel server, client and producer to this engine.
