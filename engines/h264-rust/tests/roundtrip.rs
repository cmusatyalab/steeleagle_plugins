//! Sends raw frames through a Uriel server, the encoder and the decoder, and checks the frames that
//! come out of the decoder against the ones that went in.
//!
//! It runs `h264-encode` and `h264-decode` as separate processes, like the daemon does. The
//! encoder to use is set in the environment, so the same test runs on a device with a hardware
//! encoder:
//!
//! ```sh
//! H264_TEST_ENCODER="qtic2venc control-rate=2 target-bitrate=3000000 b-frames=0" \
//!     H264_TEST_INPUT_FORMAT=NV12 cargo test --test roundtrip
//! ```
//!
//! `H264_TEST_BIN_DIR` says where the engines are, for a test built elsewhere.

use std::{
    env,
    path::PathBuf,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use h264_rust::{
    RAW_FRAME_TYPE, RGB_CHANNELS, RawFrameView,
    encode::{DEFAULT_ENCODER, DEFAULT_INPUT_FORMAT},
};
use protobuf::Message;
use uriel::{Client, Engine, FlowSpec, Producer, Server};

const CAMERA_ID: u64 = 7;
/// What the camera stamped on its frames: a `google.protobuf.Timestamp` of 1 second, as bytes.
const TIMESTAMP: [u8; 2] = [0x08, 0x01];
/// Even, as H.264 4:2:0 needs.
const WIDTH: usize = 640;
const HEIGHT: usize = 480;
/// How far the picture moves between frames, in pixels.
const MOTION_PER_FRAME: usize = 4;
/// H.264 is lossy; a smooth picture at a few Mbit/s stays well within this, out of 255.
const MAX_MEAN_ABS_ERROR: f64 = 8.0;
/// Room in a frame for its fields besides its pixels.
const FRAME_HEADROOM: usize = 4096;
const SINK_OUTPUT_SIZE: usize = 64;
const FLOW_TIMEOUT: &str = "20s";
const STEP_TIMEOUT: Duration = Duration::from_secs(20);
const POLL_INTERVAL: Duration = Duration::from_millis(100);
/// How long the decoder is frozen while frames pile up in front of it.
const FREEZE: Duration = Duration::from_secs(1);
/// Frames sent while the decoder is frozen. More than the decoder can hold, so some are skipped.
const FROZEN_FRAMES: u64 = 5;

/// Stops the engines it started, even if the test fails.
struct Engines(Vec<Child>);

impl Drop for Engines {
    fn drop(&mut self) {
        for child in &mut self.0 {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// The engine `name`, built along with this test at `built`.
fn engine_binary(name: &str, built: &str) -> PathBuf {
    env::var_os("H264_TEST_BIN_DIR")
        .map_or_else(|| built.into(), |dir| PathBuf::from(dir).join(name))
}

fn spawn(binary: PathBuf, args: &[&str]) -> Child {
    Command::new(binary)
        .args(args)
        .stdout(Stdio::null())
        .spawn()
        .expect("the engine starts")
}

/// A smooth picture that moves with `frame`, as packed RGB.
fn picture(frame: u64) -> Vec<u8> {
    let shift = frame as usize * MOTION_PER_FRAME;
    let mut pixels = Vec::with_capacity(WIDTH * HEIGHT * RGB_CHANNELS as usize);
    for y in 0..HEIGHT {
        for x in 0..WIDTH {
            let x = x + shift;
            pixels.extend([
                (x * 255 / WIDTH) as u8,
                (y * 255 / HEIGHT) as u8,
                ((x + y) * 255 / (WIDTH + HEIGHT)) as u8,
            ]);
        }
    }
    pixels
}

fn raw_frame(frame: u64) -> Vec<u8> {
    let mut raw = RawFrameView::new();
    raw.data = picture(frame);
    raw.h_res = WIDTH as u64;
    raw.v_res = HEIGHT as u64;
    raw.channels = RGB_CHANNELS;
    raw.id = frame;
    raw.camera_id = CAMERA_ID;
    raw.timestamp = Some(TIMESTAMP.to_vec());
    raw.write_to_bytes().expect("a frame serializes")
}

fn mean_abs_error(a: &[u8], b: &[u8]) -> f64 {
    let total: u64 = a
        .iter()
        .zip(b)
        .map(|(a, b)| u64::from(a.abs_diff(*b)))
        .sum();
    total as f64 / a.len() as f64
}

/// A camera, an encoder, a decoder and a sink behind one server.
struct Flow {
    producer: Producer,
    sink: Engine,
    decoder_pid: u32,
    // Kept alive for the test's length.
    _client: Client,
    _server: Server,
    _engines: Engines,
}

impl Flow {
    /// Starts the flow, with `tokens` frames of the camera's allowed in it at once.
    fn start(tokens: usize) -> Self {
        let encoder = env::var("H264_TEST_ENCODER").unwrap_or_else(|_| DEFAULT_ENCODER.to_owned());
        let input_format =
            env::var("H264_TEST_INPUT_FORMAT").unwrap_or_else(|_| DEFAULT_INPUT_FORMAT.to_owned());
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("the clock is after 1970")
            .subsec_nanos();
        let tag = format!("{}-{unique}", std::process::id());
        let (server_name, client_name) = (format!("rt-srv-{tag}"), format!("rt-cli-{tag}"));
        let (encode, decode, sink) = (
            format!("rt-enc-{tag}"),
            format!("rt-dec-{tag}"),
            format!("rt-sink-{tag}"),
        );
        let camera = format!("rt-cam-{tag}");

        let decoder = spawn(
            engine_binary("h264-decode", env!("CARGO_BIN_EXE_h264-decode")),
            &[
                "--name",
                &decode,
                "--max-width",
                &WIDTH.to_string(),
                "--max-height",
                &HEIGHT.to_string(),
            ],
        );
        let decoder_pid = decoder.id();
        let encoder = spawn(
            engine_binary("h264-encode", env!("CARGO_BIN_EXE_h264-encode")),
            &[
                "--name",
                &encode,
                "--decoder",
                &decode,
                "--encoder",
                &encoder,
                "--input-format",
                &input_format,
            ],
        );
        let engines = Engines(vec![encoder, decoder]);

        let mut server = Server::new(&server_name, None, None).expect("the server starts");
        let sink_engine = Engine::new(&sink, None, SINK_OUTPUT_SIZE).expect("the sink starts");
        // The engines register their services as they start.
        let deadline = Instant::now() + STEP_TIMEOUT;
        for engine in [&encode, &decode, &sink] {
            while let Err(e) = server.add_engine(engine) {
                assert!(
                    Instant::now() < deadline,
                    "engine {engine} never started: {e}"
                );
                thread::sleep(POLL_INTERVAL);
            }
        }

        let frame_size = raw_frame(0).len();
        let producer = Producer::new(&camera, Some(RAW_FRAME_TYPE), frame_size + FRAME_HEADROOM)
            .expect("the producer starts");
        let mut client = Client::new(&client_name, &server_name, None).expect("the client starts");
        client.add_producer(&camera).expect("the producer is added");
        let spec: FlowSpec = format!(
            r#"
            [[producers]]
            name = "{camera}"
            tokens = {tokens}

            [[engines]]
            name = "{encode}"
            location = "local"

            [[engines]]
            name = "{decode}"
            location = "local"

            [[engines]]
            name = "{sink}"
            location = "local"

            [[edges]]
            from = "{camera}"
            to = "{encode}"
            timeout = "{FLOW_TIMEOUT}"

            [[edges]]
            from = "{encode}"
            to = "{decode}"
            timeout = "{FLOW_TIMEOUT}"

            [[edges]]
            from = "{decode}"
            to = "{sink}"
            timeout = "{FLOW_TIMEOUT}"
            "#
        )
        .parse()
        .expect("the spec parses");
        client
            .add_flow("roundtrip", &spec)
            .expect("the flow is added");

        Self {
            producer,
            sink: sink_engine,
            decoder_pid,
            _client: client,
            _server: server,
            _engines: engines,
        }
    }

    /// Sends the picture of `frame`.
    fn send(&mut self, frame: u64) {
        let sent = raw_frame(frame);
        self.producer.new_input().expect("room for a frame")[..sent.len()]
            .write_copy_of_slice(&sent);
        self.producer
            .publish_input(sent.len())
            .expect("the frame is sent");
    }

    /// The next frame to come out of the decoder, if one does within `timeout`.
    fn receive(&mut self, timeout: Duration) -> Option<RawFrameView> {
        let deadline = Instant::now() + timeout;
        let decoded = loop {
            match self.sink.poll_input() {
                Ok(input) => {
                    assert_eq!(input.payload_type(), RAW_FRAME_TYPE);
                    break RawFrameView::parse_from_bytes(input.data).expect("a RawFrame");
                }
                Err(uriel::Error::Again) => {
                    if Instant::now() >= deadline {
                        return None;
                    }
                    let _ = self.sink.wait(POLL_INTERVAL);
                }
                Err(e) => panic!("the sink failed: {e}"),
            }
        };
        self.sink.drop(&[]).expect("the sink answers");
        Some(decoded)
    }

    /// Sends the picture of `frame`, and checks the frame that comes out of the decoder against
    /// it. Returns the number the encoder gave it.
    fn round_trip(&mut self, frame: u64) -> u64 {
        self.send(frame);
        let decoded = self
            .receive(STEP_TIMEOUT)
            .unwrap_or_else(|| panic!("frame {frame} never came out"));
        assert_eq!(
            (decoded.h_res, decoded.v_res, decoded.channels),
            (WIDTH as u64, HEIGHT as u64, RGB_CHANNELS),
            "frame {frame}"
        );
        assert_eq!(decoded.camera_id, CAMERA_ID, "frame {frame}");
        assert_eq!(
            decoded.timestamp.as_deref(),
            Some(&TIMESTAMP[..]),
            "frame {frame}"
        );
        let error = mean_abs_error(&decoded.data, &picture(frame));
        assert!(
            error <= MAX_MEAN_ABS_ERROR,
            "frame {frame}: mean absolute error {error:.1}"
        );
        decoded.id
    }

    /// Stops or resumes the decoder process.
    fn signal_decoder(&self, signal: &str) {
        let status = Command::new("kill")
            .args([signal, &self.decoder_pid.to_string()])
            .status()
            .expect("kill runs");
        assert!(status.success(), "kill {signal} failed");
    }
}

#[test]
fn frames_survive_the_encoder_and_the_decoder() {
    const FRAMES: u64 = 12;
    let mut flow = Flow::start(1);

    for frame in 0..FRAMES {
        // The encoder numbers the frames it encodes, and the decoder passes that on.
        assert_eq!(flow.round_trip(frame), frame);
    }
}

/// Frames the decoder never gets break the chain of P-frames after them, so the encoder must make
/// its next frame a keyframe, or nothing decodes again.
#[test]
fn stream_recovers_after_frames_are_lost() {
    const BEFORE: u64 = 3;
    const AFTER: u64 = 3;
    let mut flow = Flow::start(FROZEN_FRAMES as usize);

    for frame in 0..BEFORE {
        flow.round_trip(frame);
    }

    flow.signal_decoder("-STOP");
    for frame in BEFORE..BEFORE + FROZEN_FRAMES {
        flow.send(frame);
        thread::sleep(POLL_INTERVAL);
    }
    thread::sleep(FREEZE);
    flow.signal_decoder("-CONT");
    // Whatever the decoder still held comes out; the frames it skipped never do.
    while flow.receive(FREEZE).is_some() {}

    for frame in BEFORE + FROZEN_FRAMES..BEFORE + FROZEN_FRAMES + AFTER {
        flow.round_trip(frame);
    }
}
