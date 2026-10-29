# H.264 (Rust)

SteelEagle engine plugins that send video frames over a slow link as H.264 instead of raw pixels. The same engines as the Python H.264 plugin in `defaults/h264`, in Rust, and they speak the same messages: either plugin's encoder works with either plugin's decoder.

| Plugin | Takes | Emits |
| --- | --- | --- |
| `encoder.plugin.toml` | a raw RGB `RawFrame` | an `EncodedFrame` (`CODEC_H264`) |
| `decoder.plugin.toml` | an `EncodedFrame` | the `RawFrame` it decodes to |

A typical flow runs the encoder on the vehicle and the decoder on the remote daemon, before a detector:

```
camera → h264 encoder ══ remote daemon ══► h264 decoder → detector
```

## Args

Encoder:

| Arg | Default | Description |
| --- | --- | --- |
| `decoder_name` | `h264_decoder` | Service name of the decoder on the remote daemon, whose results tell the encoder when to send a keyframe. Not watched when there is no remote daemon. |
| `encoder` | `x264enc tune=zerolatency speed-preset=ultrafast bitrate=3000 key-int-max=300` | H.264 encoder, in gst-launch syntax. |
| `input_format` | `I420` | 4:2:0 pixel format the encoder is given: `I420` for software encoders, `NV12` for hardware ones such as `qtic2venc`. |
| `fps` | `30` | Frame rate the encoder is told the frames come at. |
| `max_output_size` | `2097152` | Largest encoded frame sent, in bytes. |
| `timeout_ms` | `1000` | Milliseconds a frame may take to encode before it is discarded. |
| `log_frames` | `false` | Log every frame encoded. |

Decoder:

| Arg | Default | Description |
| --- | --- | --- |
| `decoder` | `avdec_h264 max-threads=1` | H.264 decoder, in gst-launch syntax. |
| `max_width`, `max_height` | `1920`, `1080` | Largest frame decoded, in pixels. |
| `timeout_ms` | `1000` | Milliseconds a frame may take to decode before it is discarded. |
| `log_frames` | `false` | Log every frame decoded. |

## Keyframe on gap

Uriel keeps results fresh by skipping stale frames, and a crossing may time a frame out. But an H.264 P-frame only decodes after every frame before it, back to a keyframe. So:

- The encoder numbers the frames it encodes. After a gap in the numbers, the decoder drops frames until the next keyframe, and its result says it needs one.
- The encoder subscribes to the decoder's events. When a frame it encoded since its last keyframe is skipped, times out, or does not decode cleanly, it makes its next frame a keyframe. A skip at the crossing is known on the vehicle at once, so the keyframe usually comes on the very next frame.
- Each camera is a stream of its own, with its own frame numbers and keyframes, so cameras of different sizes never break each other's streams.
- The decoder's result is `h264/v1/DecodeResult`, the same as the Python plugin's.

## Frames and their fields

The encoded frame is SteelEagle's `EncodedFrame`. The encoder copies the raw frame's timestamp, gimbal, position and camera id to it, and the decoder copies them back. The messages in them pass through as bytes. `proto/h264.proto` has the fields the engines use, with SteelEagle's field numbers.

What an `EncodedFrame` has no room for does not survive: any other field of the `RawFrame`, such as `d_res`, is lost. The `id` of the encoded frame counts the frames encoded for a camera, so the decoded `RawFrame` gets that number as its `id`, not the one the camera gave it.

The pixels go to the encoder as 4:2:0 (`input_format`, I420 by default), the only kind browsers and hardware decoders take. Without a format, x264 would pick 4:4:4. Hardware encoders take NV12, which is 4:2:0 too.

## Building and running

The plugin runs on two kinds of machine, and `pixi run start` does the right thing on each:

- **linux-64, for development.** Pixi compiles the engines against conda-forge's GStreamer, which comes with the environment, and Cargo fetches Uriel from its repository, so nothing is installed system-wide.
- **linux-aarch64, a device such as a Particle Tachyon.** Pixi downloads the engines that CI built (see [On a device](#on-a-device-aarch64)). They link the device's own GStreamer.

```sh
pixi run build      # once, so the first start is quick
pixi run test       # linux-64 only
pixi run start      # what the daemon runs; see below
```

The daemon starts the plugin with `pixi run start`, which runs the encoder or the decoder as `STEELEAGLE_PLUGIN` says, with the args in `STEELEAGLE_PLUGIN_ARGS`, the engine name in `STEELEAGLE_ENGINE_ENDPOINT`, and the remote daemon in `STEELEAGLE_REMOTE_QUALIFIER`. Keep Uriel's version in `Cargo.toml` in step with the daemon's, since they share memory.

The engines also run on their own, without a daemon, for development:

```sh
pixi run cargo run --release --bin h264-decode
pixi run cargo run --release --bin h264-encode -- --encoder "openh264enc bitrate=3000000"
```

`--log-frames` prints a line per frame: its number, whether it is a keyframe, its size, and how long it took. `--help` lists the rest.

The encoder must not hold frames back: each frame's output has to come out before the next frame goes in. Turn off B-frames and lookahead, as the defaults do. A frame whose output does not come within `timeout_ms` is discarded.

## On a device (aarch64)

A device's hardware encoder (`qtic2venc` on a Tachyon) is a plugin of the device's own GStreamer, which a conda-forge GStreamer does not load. So the device's pixi environment holds no GStreamer, only `curl`, and its `build` task (`tools/fetch-binary.sh`) downloads the engines from the GitHub release `h264-rust-v<version>` of this repository, checks them against the release's checksum, and installs them in `bin/`. The version is the one in `Cargo.toml`, and `bin/` remembers which it has, so a start only downloads after the version changes. The download is the only network access a start needs. `H264_RUST_RELEASE_URL` replaces the directory it downloads from.

The releases are built against Ubuntu 20.04's GStreamer 1.16 and glibc 2.31 (`Dockerfile.aarch64`), and the engines link `libgomp` at startup, because GStreamer's libav plugin fails to load on aarch64 without it. A device with older libraries cannot run them.

In the daemon's config:

- **Leave the plugin's `isolation` unset.** A sandbox gets a minimal `/dev` and a container gets no devices, so neither reaches the encoder's device nodes.
- **Use the hardware encoder and its pixel format.** `qtic2venc` and `v4l2h264enc` take NV12 only:

  ```toml
  encoder = "qtic2venc control-rate=2 target-bitrate=3000000 b-frames=0"
  input_format = "NV12"
  ```

- **Give the daemon's user access to the devices.** On a Tachyon, `/dev/video*` is group `video` and `/dev/dri/renderD128` is group `render`.

### Making a release

1. Raise `version` in `Cargo.toml` and run `cargo update -p h264-rust --offline` so `Cargo.lock` follows.
2. Tag the commit `h264-rust-v<version>` and push the tag.

`.github/workflows/h264-rust.yml` tests the plugin, cross-compiles it, and publishes the tarball and its checksum as the release. The workflow refuses a tag that is not the version in `Cargo.toml`. `tools/package-release.sh` does the build part locally, into `dist/`.

### What has been tried on a Tachyon

Tried on a Tachyon (Ubuntu 20.04, GStreamer 1.16.3), with the release files served from a local directory instead of GitHub:

- `pixi run start` builds the device environment, installs the engines, and runs the plugin. The running process uses the system GStreamer and nothing from the pixi environment.
- The encoder starts with the `qtic2venc` args, and a wrong plugin name is refused.
- With `qtic2venc`, frames come out of the decoder correctly (`tests/roundtrip.rs`), and after frames are lost the encoder's next frame is a keyframe, through `qtic2venc`'s `trigger-iframe` action. That test is built with `cargo test --no-run --target aarch64-unknown-linux-gnu --test roundtrip` in the Dockerfile's image, and run with `H264_TEST_BIN_DIR` set to the directory of the engines.
- A corrupted download is refused and installs nothing.

Not tried: downloading from a real GitHub release, running under the new eagled, and running as a user without root. `v4l2h264enc` does not work there. Software `x264enc` runs, at about 20 fps at 720p and 12 fps at 1080p, with the device already busy.
