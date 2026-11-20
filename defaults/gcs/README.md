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
`ice_servers`, `webrtc_ports`, `max_input_size`, `compiler`, `dump_dir`,
`prometheus_url`, `ai`).

Keys are never args, which sit in committed configs. The GCS reads them from
files in the daemon's files directory, `$STEELEAGLE_FILES` (for a root daemon,
`/root/.local/share/steeleagle/files`): the Mapbox token from `.mapbox`, and
the OpenAI API key from `.openai` (used unless a key is saved in the AI panel
or `OPENAI_API_KEY` is set). Without `.mapbox` the maps have no basemap.

```sh
umask 077; printf '%s' "$MAPBOX_TOKEN" > ~/.local/share/steeleagle/files/.mapbox
```

To debug the video stream, set `dump_dir` to record every H.264 frame the GCS
receives, before WebRTC, as `<producer>-<time>.h264` (play it with `ffplay`)
and `<producer>-<time>.csv` (one row per frame: arrival and capture times,
Uriel sequence number, frame id, keyframe, size, and frames missed before it).
Each gap in frame ids is also logged. Gaps mean frames were lost before the GCS,
for example skipped by Uriel; a clean recording with blocky video in the
browser points at WebRTC or the browser.

### Encoder service

Every daemon that runs a vehicle must also run an engine service with the
`encoder` plugin of the `h264` package (`defaults/h264`), under any service
name. The GCS builds each vehicle's flow as camera, encoder, GCS, using the
encoder on the vehicle's own daemon; if a daemon runs more than one, the first
by name. A vehicle on a daemon without one (or without a camera) gets a
telemetry-only flow.

The GCS pushes this flow when a vehicle connects and after each hold, and pushes
it again, backing off from 5 s to 30 s, until the vehicle's telemetry (and
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
the engines with their input, forwarded and result types and the producers they
advertise (503 without a daemon).

An engine's `forwards` sets what its output edges carry: `"*"` (the default)
passes on whatever input types reach it, a specific input type passes on
only that type, and any other type means the engine converts each input into
it, as an encoder (`forwards` EncodedFrame) or a decoder (`forwards` RawFrame)
does. Other new data an engine makes leaves through one of its
`[[engine.producers]]`. The Flow Builder lists each
engine's producers under it, and type-checks their edges by their declared
output type. A flow names an engine's producer `{host}.{engine}.{producer}`.
Only the producers of engines on the vehicle's own daemon can be used: Uriel's
producers run with the vehicle's client, so the Flow Builder doesn't offer the
producers of the orchestrator's engines, and Deploy and the vehicle refuse them.

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

### AI Chat and Inferred Mission

The Plan page has **Chat** and **Inferred Mission** tabs. Chat uses the GCS's
Go NL2DSL service. Inferred Mission V7 recommends a continuation and explains
which observations support it. Uploaded MP4s use sampled images and their
timestamps. Live observations can add matched detections and telemetry. The
model can infer tracking from the images before GCS checks the vehicle's
available camera and detector routes.

GCS uses the V4 schema and compiler from Mission Builder and presents one
compiled candidate. When the evidence is unclear or the proposed continuation
cannot be bound or compiled, GCS tries **Wait 30 seconds, then return home**
for the selected vehicle. The fallback comes from policy. Plan retains the
vision recommendation and its reason for review. **Apply to Mission Builder**
opens the compiled candidate on the canvas. Use Map, Flow Builder, Compile and
Deploy to finish the mission. Link-loss activation requires a mission already
loaded on the vehicle and a separate onboard trigger.

See [Detector evidence in Inferred Mission](internal/inferredmission/detector-context.md)
for live detector requirements and Aviary verification.

In **Orchestrate → Configure**, open the GCS service and expand **AI**. Open
the GCS directly through `localhost` or `127.0.0.1` on its host to save,
replace, or clear provider keys. A local SSH port forward to the same GCS
also works. Browsers connected over the network can view credential status.
New provider requests use the saved key immediately. The GCS stores it in a
service-specific file with mode `0600` under
`~/.config/steeleagle/gcs/credentials` (or `$XDG_CONFIG_HOME/steeleagle/gcs/credentials`).
The form shows credential status and never reads back key values.

OpenAI Chat and video inference use the OpenAI key. Anthropic Chat uses the
Anthropic key, and vision still uses OpenAI. `OPENAI_API_KEY` and
`ANTHROPIC_API_KEY` in the GCS process environment remain fallbacks when
there is no UI-saved key, including after **Clear saved key**. For services
started with `eagle init`, the private `~/.config/steeleagle/eagled.env`
file can provide these variables when the daemon starts. Keep key values out
of plugin args and Raw TOML.

Use **Apply** for provider, model, and inference settings. New runs use V7 and
local mission templates. Resumed runs retain their recorded prompt. V6 uses
local mission templates and V5 uses the Chat model for DSL generation. Use the
main video settings to adjust sections, sampling, and history.
**Advanced inference settings** contains
image size, request timeouts, video upload limits, and the active-run limit.
`chat_temperature` and `vision_temperature` are optional, in the range
0–1. An omitted field uses the provider default. Leave temperature blank
for reasoning models such as `gpt-6.1-sol` and for providers or models that
do not support it. Configure and the Plan tabs distinguish saved settings
from the models currently in use.

```toml
[services.plugin.args.ai]
chat_provider = "openai"
chat_model = "gpt-6.1-sol"
chat_reasoning_effort = "medium"
vision_model = "gpt-6.1-sol"
vision_reasoning_effort = "medium"
data_dir = "/var/lib/steeleagle/gcs-analysis"

[services.plugin.args.ai.inferred_mission]
section_mode = "non_overlapping"
section_seconds = 15
frame_fps = 1
history_mode = "recency"
history_sections = 10
half_life_sections = 3
```

The GCS process must be able to write to `data_dir`. It defaults to
`steeleagle/gcs/inferred-mission` within the service user's configuration
directory. `STEELEAGLE_INFERRED_MISSION_DATA` can override it. Use a distinct
data directory for each GCS instance. For a trusted model gateway, set
`OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`, or
`STEELEAGLE_VISION_BASE_URL` to the provider's versioned base URL.

Use **Advanced options → Section window** in a new run to override the
configured section mode. The default is consecutive 15-second windows at
1 fps. To reproduce the sliding experiment, set `section_mode = "sliding"`,
`section_seconds = 5`, and `frame_fps = 1`. Windows start one second apart.
Sliding MP4 runs share up to five concurrent section workers; live runs
process their overlapping windows in order.

The report shows wall time for the run and timing for each section. Rename
or archive an idle run from Saved runs; restore it before resuming. Archiving
keeps the source, sampled frames, and records. JSON downloads use the run's
name, UTC creation time, and section index.

Install FFmpeg and ffprobe for both MP4 and live analysis. Live Observe lists
currently streaming V4 cameras. Keep the camera → encoder → GCS and
telemetry → GCS edges in the active dataflow. A live run can also use a
reachable local detector on that camera's active flow. Detector labels are
matched to sampled frames by their transport identity. History shows the
matched evidence and available motion telemetry. Ordinary MP4 runs use
their own frames and can resume after a restart. Saved live runs remain
available for review. The graph JSON download uses the V4 format and carries
a schema version.

For local verification, run `pixi run mock` and open the Plan page at
`http://localhost:8002`. Test a short MP4 and a mock camera separately.
The mock supplies frames and telemetry. A draft referencing a detector or
map feature requires the corresponding Flow Builder or Map configuration
before compilation. Detector behavior requires a real detector.

#### Live tracking with a detector and the cliped verifier

A live run can follow box_rerank's target and keep the vehicle's fallback
mission current:

```toml
[services.plugin.args.ai.inferred_mission]
verifier = "qwen3-vl:30b-a3b-instruct" # the cliped verifier's Ollama model
verifier_live_dir = "/root/verifier_live"
auto_deploy = true
```

- When starting a live run, choose two detection engines from the vehicle's
  daemon (`local.`) and the GCS's (`remote.`). **Detector** (on the GCS's
  daemon, e.g. RF-DETR) gives the model each sampled frame's boxes (up to 8,
  best first), numbered and tied to the frame's image. **Track detector**
  (e.g. `local.box_rerank`) is the engine the Track mission follows its target
  with; it must be connected to the camera in the planning flow. Left on
  automatic, they are the one detector in the vehicle's active flow and the
  one in the planning flow. `detector` (a full engine name) and
  `local_detector` (a flow name) in `inferred_mission` set defaults.
- When the model recommends Track, it names the box of the object the pilot
  is following (`target_box`); framing is imperfect, so it need not be the
  centered one. The box's detector class becomes the Track class. The model
  never names an engine: the GCS binds the Track detector.
- With `verifier`, the GCS asks that model (at `OLLAMA_HOST`, else
  `localhost:11434`) the verifier's box question about that box, against the
  prompt in `verifier_live_dir`: the verifier's own text of it,
  `description.txt`, if the verifier has written it since the prompt changed,
  else `prompt.txt`, or the GCS's description of `prompt.jpg`. If it says no, or there is no prompt, it writes a crop of the
  box to `prompt.jpg` (an image wins over `prompt.txt`), setting any
  `prompt.jpeg` aside, with the box's detector class just before it as
  `class.txt`, which the verifier takes as the image's kind (its class prompt
  for box_rerank's gate). A prompt a tracked box has matched is replaced only
  after two mismatches in a row, so one section whose framing the vision
  model misread doesn't undo it; the check records the deferred mismatch. The model also names `distractor_boxes`, up to three
  other detections of the target's class in the section (e.g. bystanders); the
  GCS writes their crops beside the prompt as `d1.jpg`, `d2.jpg`, ... after
  setting the old target's aside (and, while the prompt stays, only if there
  are none yet, since each change restarts box_rerank's learning). The
  verifier turns the prompt into text (give it `image_mode = "contrastive"`,
  so the text names the colors and features that set the box apart from the
  distractors, or `"descriptive"` for a description of the box alone, rather
  than just its kind), and box_rerank follows its heartbeat. The section's
  History records the check; `target.png` beside its record is the crop
  checked, `new_prompt.jpg` the one written, and `distractor_N.jpg` the
  distractors.
- `auto_deploy` is off by default. When on, each live section's compiled
  mission (including the wait-and-return fallback) is sent to the vehicle with
  `CompileMission`, compiled there as `inferred_mission` and loaded, and the
  vehicle's data failsafe is set to start it (`FAILSAFE_BEHAVIOR_MISSION`). The
  vehicle needs `eaglec`, and refuses while a mission is running.

#### Headless: `inferred_mission_headless`

The package's second plugin runs the same live inference without the web UI.
It observes one camera from its first frame, logs each section's mission
(`inferred mission section`), keeps runs under `inferred_mission_headless` in
`$STEELEAGLE_FILES`, and with `auto_deploy` sends each mission to the vehicle.
It exits unless `$STEELEAGLE_FILES/.openai` holds the OpenAI key. The planning
flow is a TOML file (`flow`), named relative to the vehicle, so Track can bind
`local_detector` to the camera:

```toml
[[services]]
  name = "inferred_mission"
  type = "engine"
  [services.plugin]
    package = "https://github.com/cmusatyalab/steeleagle_plugins//defaults/gcs@v4_backend_rework"
    plugin = "inferred_mission_headless"
    [services.plugin.args]
      vehicle = "drone"
      camera = "front"
      flow = "box_rerank_tracking.flow.toml"   # in $STEELEAGLE_FILES
      detector = "detection"
      local_detector = "local.box_rerank"
      verifier = "qwen3-vl:30b-a3b-instruct"
      verifier_live_dir = "/root/verifier_live"
      auto_deploy = true
```

Give it the camera's H.264 frames and the vehicle's telemetry in the active
flow, as the GCS gets them.

### Flows in missions

A flow designed on the Flow Builder tab ships with Deploy: the Plan page sends it
with the mission, and Deploy resolves it for each vehicle and builds it into the
mission as the mission's flow and manifest (vehicles that build the same share a
binary). The flow and manifest name every node relative to the vehicle, which
qualifies the names itself, so the bundle holds no addresses:

| Node | Bundled name | The vehicle qualifies it to |
|---|---|---|
| A stream of the vehicle | `vehicle.front`, `vehicle.telemetry` | `{vehicle}.front` |
| A local engine, on the vehicle's daemon | `local.yolo` | `{daemon IP}.yolo` |
| A remote engine, on the orchestrator (the GCS's daemon) | `remote.h264_encoder` | `{orchestrator IP}.h264_encoder` |
| A local engine's producer | `local.enc.frames` | `{daemon IP}.enc.frames` |

Whether an engine is local or remote depends on the target: an engine on the
orchestrator is `remote.yolo` for most vehicles, but `local.yolo` for a vehicle
on the orchestrator's own daemon. A vehicle whose daemon doesn't run the
flow's engines fails with `flow engine ... is neither on this vehicle's daemon
nor on the orchestrator`. The GCS's own flow (camera, encoder, GCS) uses the
same names. The vehicle stores an engine's results under its flow name, so a
mission asks for `local.yolo`'s results.

A mission action can name a flow engine, such as the `Engine` of `Track`, by the
template's name for it; Deploy builds it into each target's mission by the
engine's name in that target's flow (`DetectionEngine_local_yolo`). The manifest declares each engine's result type. The SDK
only generates typed engine fields for detection, classification, guidance,
position and generic results, so an engine with any other result type is
declared with the Generic stand-in (`messages/result/GenericResult`).

The end-to-end tests are opt-in: `GCS_COMPILER_E2E=1 pixi run go test
./internal/mission/ ./cmd/gcsmock/` builds real missions (including one with a
flow, and a mock Deploy that sends it), which needs the same access.

## Orchestrate page

The Orchestrate page lists every daemon the GCS's daemon knows of: itself and
the remote daemons that use it as their orchestrator. A green dot is up, orange
is up but its services failed to start (it can still be configured, which is
how it is fixed), red is unreachable. Every call for a daemon goes through the
GCS's daemon with that daemon's IP as its route key.

For the selected daemon:

- **Configure...** opens its whole config as a form (daemon settings, the
  simulator, and its services) or as raw TOML. A service's plugin is picked from
  the packages installed on that daemon, and its args are prefilled from the
  plugin's TOML (`[[args]]`: type, description, default). Args already in the
  config are always kept; others are written only if changed from their default.
  Keys the form doesn't know are kept, but the form doesn't keep TOML comments;
  raw TOML is sent exactly as typed.
- **Apply** checks the config first: the daemon's own validation, and warnings
  for services whose package isn't installed there (a daemon with such a service
  starts none of its services). On the GCS's own daemon those block Apply,
  because a daemon whose services fail takes the GCS down with it, and it can
  then only be fixed with `eagle configure` on the host. Removing or renaming
  the GCS's own service needs an extra confirmation.
- **Restart Daemon** restarts the daemon and every service on it.
- **Services** lists the daemon's services, each of which can be restarted or
  stopped. A stopped service shows as "Stopped by request" and stays stopped,
  even when its daemon restarts, until **Start** starts it again; one that
  exited shows as "Stopped". The GCS can't stop its own service, since nothing
  could start it again from the page.
- **Packages** lists the installed packages and their plugins, and installs (by
  URI, `repo[//subdir][@version]`) or removes them. Changes take effect when the
  daemon next restarts.

Configure, Restart Daemon and package changes need the GCS service to have
`auth = "admin"` (see above); without it the daemon refuses them, and the page
says so. On a remote daemon they also need that daemon to trust its
orchestrator's address. After an Apply or a restart, the page follows the daemon
until it is back, or reports that its services failed or that it didn't come
back within a minute.

## Metrics page

The Metrics page draws a vehicle's running flow, whichever flow it runs, as a
graph, with each engine's numbers over the last 1, 5 or 15 minutes or hour:
time per input (p50, p95 on hover), wait before the engine, inputs per second,
and the share of inputs skipped. On each path from a producer, the busiest
engine is marked as the bottleneck when it is busy at least 80% of the time or
skips at least 5% of its inputs. Above the graph is the end-to-end time from capture to the GCS;
engine times don't add up to it, since capture, transfer and queueing aren't
engine time.

Edges that cross the network, between an engine on the vehicle and one on the
remote server, are dashed and labeled with the mean round trip time. When the
flow has any, the page also shows the round
trip time between the vehicle's daemon and its remote server
(`uriel_remote_rtt_seconds`, QUIC's smoothed estimate), as its minimum and mean
over the window. The minimum is close to the round trip propagation delay; it
doesn't include the time to send a frame, which grows with its size over the
bandwidth.

The numbers come from a Prometheus server, the `prometheus` plugin of
`engines/prometheus`, run as an `extra` service on the GCS's daemon:

```toml
[[services]]
name = "prometheus"
type = "extra"
[services.plugin]
plugin = "prometheus"
package = "github.com/cmusatyalab/steeleagle_plugins//engines/prometheus"
```

- It scrapes every daemon that sets `[daemon] metrics_port`, which it learns
  from the GCS at `GET /api/metrics/targets` (Prometheus HTTP service
  discovery), so new daemons need no change to it.
- It scrapes the GCS's own `GET /metrics`, whose `gcs_frame_age_seconds`
  histogram is each frame's age on arrival (arrival minus the capture time the
  vehicle stamped; frames that seem to arrive before capture, from clock skew,
  count in `gcs_frame_age_negative_total` instead).
- The GCS queries it at the `prometheus_url` arg (default
  `http://localhost:9090`) for `GET /api/metrics/flow?vehicle=&window=`. While
  it can't be reached, the page shows the flow without numbers; video and
  control don't depend on it.

`PROMETHEUS_URL=http://localhost:9090 pixi run go test ./internal/metrics/`
also checks every query against that server.

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
- keeps a config and packages for each daemon: Configure validates and stores
  the config, and the daemon then lists no services for 3 s, as if restarting;
  a config naming a package the daemon doesn't have makes its services "fail to
  start" until it is fixed. Package installs take effect at once. The listed
  services still come from the flags, not the config;
- runs the real `bin/gcs` as its child, as the daemon runs an engine plugin.

Run it in `gcs/` and open <http://localhost:8002>:

```sh
pixi run mock
```

The mock reads `gcs.plugin.toml` from this checkout so Configure shows the
same declared args as the installed plugin. Set `XDG_CONFIG_HOME` to a
separate directory when testing saved credentials in the mock. To use custom non-secret
settings in the running GCS, set the API key in the shell environment and
pass a file containing the `ai` object directly:

```sh
pixi run mock -ai-config /path/to/gcs-ai.json
```

For example, `gcs-ai.json` can contain:

```json
{
  "chat_model": "gpt-4.1",
  "chat_temperature": 0.2,
  "vision_model": "gpt-6.1-sol",
  "inferred_mission": {
    "section_mode": "sliding",
    "section_seconds": 5,
    "frame_fps": 1,
    "history_mode": "recency"
  }
}
```

The file seeds the mock's Configure form and the GCS process at startup.
Configure's Apply operation still simulates the daemon restart. To change
the mock's running settings, edit the file and restart the mock.

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
| `-deny-config-daemons` | none | daemon IPs that refuse Configure, Restart and package changes with PermissionDenied, as a daemon does for a GCS without admin auth |
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
