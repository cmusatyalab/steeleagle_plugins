# SteelEagle GCS

The ground control station: a Go server and web UI. It runs as an engine plugin
under `eagled`, receives frames and telemetry from dataflows, streams them to
browsers over WebRTC, and controls vehicles through the daemon.

## Running under eagled

Add the GCS as an engine service in the daemon's config. It needs `auth = "admin"`
so it can list and control vehicles and set dataflows:

```toml
[[services]]
name = "gcs"
type = "engine"
auth = "admin"
[services.plugin]
plugin = "gcs"
package = "github.com/cmusatyalab/steeleagle_plugins//gcs"
[services.plugin.args]
http_addr = ":8002"
```

The daemon passes the plugin args in `STEELEAGLE_PLUGIN_ARGS`, the service's
Uriel engine name in `ICEORYX_SERVICE`, and its socket in `CLIENT_SOCKET`. See
`config/examples/config.toml` and `gcs.plugin.toml` (args: `http_addr`,
`ice_servers`, `webrtc_ports`, `max_input_size`).

### Encoder service

Every daemon that runs a vehicle must also run an H.264 encoder engine service
named exactly `h264_encoder`. The GCS builds each vehicle's flow as camera,
encoder, GCS, using the encoder on the vehicle's own daemon. A vehicle on a
daemon without that service (or without a camera) gets a telemetry-only flow.

## Mission dataflows

Mission flows replace the GCS flow while a mission runs. As a mission-design best
practice, every mission flow should include an edge from `telemetry` to the GCS
engine. For video during the mission, it should also include the camera, encoder,
GCS path. Without those edges the GCS shows no video (and gets no telemetry over
the WebRTC data channel) until the vehicle is put on hold, which restores the GCS
flow. The vehicle list is unaffected: it reads telemetry from the daemon,
independently of the flow.

## Pixi tasks

Run these in `gcs/`. The package builds on its own: `go.mod` takes `api/go` and
`config` from steeleagle as tagged versions, and the `uriel` task downloads Uriel's
prebuilt native library (the release `go.mod` requires) into the Pixi environment.

- `pixi run build`: build the web UI, install liburiel (if missing) and `bin/gcs`.
- `pixi run start`: build, then run the server (what the daemon invokes).
- `pixi run test`: Go tests for the whole module (builds the server and web UI first).
- `pixi run web-test`: web UI unit tests (vitest).
- `pixi run web-dev`: web UI dev server with live reload, against a GCS on `:8002`.
- `pixi run mock`: build, then run the GCS under the mock harness (below).

## Mock harness

`cmd/gcsmock` exercises the GCS without vehicles or a daemon. It is a fake
`eagled` that:

- serves the daemon's API on a unix socket for simulated vehicles;
- hosts a real Uriel server with each vehicle's `front` camera and `telemetry`
  producers, and mock `h264_encoder` engines that answer camera frames with a
  generated H.264 test clip (ffmpeg, cached in `bin/mockclips/`);
- runs the real `bin/gcs` as its child, as the daemon runs an engine plugin.

Run it in `gcs/` and open <http://localhost:8002>:

```sh
pixi run mock
```

Ctrl-C stops the mock and the GCS. By default there are three vehicles:
`100.64.0.1.alpha` and `100.64.0.1.bravo` on the GCS's daemon (`100.64.0.1`),
and `100.64.0.2.charlie` on another daemon. Each daemon has an encoder.

Flags (`./bin/gcsmock -h`; `pixi run mock` passes `-gcs` and `-web`):

| Flag | Default | |
|---|---|---|
| `-gcs` | `bin/gcs` | the GCS binary |
| `-web` | `web/dist` | the built web UI, passed to the GCS |
| `-http` | `:8002` | where the GCS serves the web UI |
| `-socket` | `$XDG_RUNTIME_DIR/gcsmock-<pid>.sock` | the mock daemon's socket |
| `-clips` | `bin/mockclips` | where the video clips are cached |
| `-gcs-ip` | `100.64.0.1` | the IP of the GCS's daemon |
| `-vehicles` | `100.64.0.1.alpha,100.64.0.1.bravo,100.64.0.2.charlie` | vehicle names: each a daemon IP, a dot and a name |
| `-no-encoder-daemons` | none | daemon IPs without an `h264_encoder` |

Uriel names are shared by every process on the machine, so two mocks (or a mock
and a real daemon) running at once need different `-gcs-ip`, `-vehicles` and
`-http`.

Scenarios to try:

- **Fly.** Select a vehicle on the Control page, take off, and fly with the
  keyboard or a gamepad. Land and return-to-home work too. A vehicle that gets
  no joystick input for 3 s holds.
- **A mission takes the video.** Start a mission: the vehicle flies a square, and
  its flow becomes telemetry to a mission sink, with no camera, so its video
  stops and the video panel shows the no-video label.
- **Hold brings it back.** Press Esc (hold). The mission stops, the GCS pushes
  the vehicle's GCS flow again, and the video returns.
- **No encoder.** `./bin/gcsmock -no-encoder-daemons 100.64.0.2` leaves charlie's
  daemon without an encoder, so charlie gets a telemetry-only flow and the UI
  shows the no-video reason.

What the harness doesn't exercise:

- the daemon's permissions and laws, package install and orchestrator
  registration;
- real `eagled` routing between daemons and tsnet;
- remote services: the mock rewrites every flow's remote engine locations to
  local, since it runs everything in one process;
- real encoding: the encoder replays a pre-encoded clip.

`go test ./cmd/gcsmock/` (in `pixi run test`) includes an end-to-end test that
runs the mock and checks this loop: the vehicles are listed, their video
arrives, a mission takes it, hold brings it back, and the joystick moves a
vehicle. It needs `pixi run build` first, and skips without `bin/gcs`.
