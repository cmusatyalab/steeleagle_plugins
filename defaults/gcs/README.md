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
Uriel engine name in `STEELEAGLE_ENGINE_ENDPOINT`, and its socket in `STEELEAGLE_CLIENT_SOCKET`. See
`config/examples/config.toml` and `gcs.plugin.toml` (args: `http_addr`,
`ice_servers`, `webrtc_ports`, `max_input_size`, `compiler`).

### Encoder service

Every daemon that runs a vehicle must also run an engine service with the
`encoder` plugin of the `h264` package (`defaults/h264`), under any service
name. The GCS builds each vehicle's flow as camera, encoder, GCS, using the
encoder on the vehicle's own daemon; if a daemon runs more than one, the first
by name. A vehicle on a daemon without one (or without a camera) gets a
telemetry-only flow.

The GCS pushes this flow when a vehicle connects and after each hold, and pushes
it again, backing off from 2 s to 30 s, until the vehicle's telemetry (and
frames, if the flow carries video) reach the GCS. It stops as soon as they do,
or as soon as a mission is started on the vehicle, and it never replaces a flow
with engines other than the GCS and the encoder.

## Mission dataflows

Mission flows replace the GCS flow while a mission runs. As a mission-design best
practice, every mission flow should include an edge from `telemetry` to the GCS
engine. For video during the mission, it should also include the camera, encoder,
GCS path. Without those edges the GCS shows no video and gets no telemetry until
the vehicle is put on hold, which restores the GCS flow. The vehicle list also
reads its telemetry from the flow, never from the daemon, so a vehicle whose
mission flow sends the GCS no telemetry shows as disconnected until the hold.

## Flow catalog and TOML import

`GET /api/flows/catalog` lists, per daemon, the vehicles with their streams and
the engines with their input and result types (503 without a daemon).
`POST /api/flows/from_toml` with `{"toml": "..."}` parses the TOML of a flow
config into a flow template without a vehicle; it needs no daemon. It answers 422
with a `detail` for TOML that isn't a flow config (unknown keys, such as an old
`[gabriel.flow]` export) or has no producers or engines.

## Missions

The GCS compiles missions (`/api/schema`, `compile`, `render_dsl`, `parse_dsl`,
`build`) and uploads or deploys them to vehicles through the daemon (`/api/upload`,
`/api/deploy`, which need a daemon socket). Compiling is configured by the
`compiler` plugin arg:

```toml
[services.plugin.args.compiler]
timeout_s = 120
modules = [{ path = "example.com/mymodule", version = "v1.0.0" }]
imports = [{ path = "example.com/mymodule/actions", alias = "myactions" }]
```

- `modules`: extra Go modules to build missions with (`path`, `version`, and an
  optional `override` of the module's location);
- `imports`: packages whose actions and events missions may use (`path`, and an
  optional `alias`);
- `timeout_s`: how long one compile may take;
- `sdk_version`: the steeleagle SDK version missions are built against.

Compiling runs `go build`, so the first build needs network access (`GOPROXY`)
or a warm Go module cache. If the config is invalid, or the compiler can't
start, the server still runs and the compiler routes answer 503 with the reason.

### Flows in missions

A flow designed on the Flow Builder tab ships with Deploy: the Plan page sends it
with the mission, and Deploy resolves it for each vehicle and builds it into the
mission as the mission's flow and manifest (vehicles that build the same share a
binary). A flow engine is either local or remote:

- a local engine runs on the vehicle's own daemon. It is written with its bare
  name (`yolo`), and Deploy qualifies it with the vehicle's daemon IP
  (`100.64.0.2.yolo`), so a vehicle whose daemon doesn't run it fails with
  `flow needs local engine yolo, which daemon 100.64.0.2 doesn't run`;
- a remote engine runs on the orchestrator (the GCS's daemon). It is written
  with its full service name (`100.64.0.1.h264_encoder`) and is the same for
  every vehicle.

A mission action can name a flow engine, such as the `Engine` of `Track`, by the
template's name for it. The manifest declares each engine's result type. The SDK
only generates typed engine fields for detection, classification, guidance,
position and generic results, so an engine with any other result type is
declared with the Generic stand-in (`messages/result/GenericResult`).

The end-to-end tests are opt-in: `GCS_COMPILER_E2E=1 pixi run go test
./internal/mission/ ./cmd/gcsmock/` builds real missions (including one with a
flow, and a mock Deploy that sends it), which needs the same access.

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
- lists a `yolo` detection engine (RawFrame in, DetectionResult out) on every
  vehicle daemon, so the flow catalog has an engine to offer. Nothing runs it:
  it is only listed. Every listed engine carries a real engine plugin config;
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
| `-no-yolo-daemons` | none | daemon IPs without a `yolo` engine |
| `-daemon-arch` | `amd64` for every daemon | `ip=arch[,ip=arch]` (`amd64` or `arm64`): the architecture a daemon reports. Only daemons other than `-gcs-ip` are reported as remotes, as with real eagled. E.g. `-daemon-arch 100.64.0.2=arm64` tests "no binary for arm64" |

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
- **Missing engine.** `./bin/gcsmock -no-yolo-daemons 100.64.0.2` leaves charlie's
  daemon without `yolo`, to see a flow that needs a local engine its daemon
  lacks.
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
