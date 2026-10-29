//! The encoder engine: encodes raw RGB frames (SteelEagle `RawFrame`s) with H.264, and emits
//! each as a SteelEagle `EncodedFrame`, e.g. for the decoder on a remote server.
//!
//! Frames reach the decoder only if Uriel carries them there: the crossing to the remote server
//! may skip a frame for a newer one, or time it out. Each P-frame needs the frames before it, so
//! the encoder subscribes to the decoder's events, and makes its next frame a keyframe once a
//! frame it encoded since its last keyframe does not reach the decoder.
//!
//! Each producer gets an H.264 stream of its own: its own pipeline, frame numbers and keyframes.
//! A frame refers to the frames before it in its stream only, so producers never break each
//! other's streams.

use std::{
    collections::HashMap,
    sync::atomic::AtomicBool,
    time::{Duration, Instant},
};

use crate::{
    Codec, DecodeResult, ENCODED_FRAME_TYPE, EncodedFrameView, Pipeline, RAW_FRAME_TYPE,
    RGB_CHANNELS, RawFrameView, WAIT_TIMEOUT, stopped,
};
use anyhow::{Context, Result};
use gstreamer::{self as gst};
use gstreamer_video::{VideoFormat, VideoInfo};
use protobuf::Message;
use uriel::{
    Engine, EventKind, InputId,
    subscription::{Event, Published},
};

/// Software x264, tuned so that each frame comes out before the next goes in. videoconvert gives
/// the encoder the pixel format it takes. On a Tachyon, the hardware encoder:
/// "qtic2venc control-rate=2 target-bitrate=3000000 b-frames=0".
pub const DEFAULT_ENCODER: &str =
    "x264enc tune=zerolatency speed-preset=ultrafast bitrate=3000 key-int-max=300";
/// The frame rate the encoder's rate control assumes, unless told otherwise.
pub const DEFAULT_FPS: u32 = 30;
/// The pixel format the encoder is given, unless told otherwise. Without one, x264enc would be
/// given 4:4:4, which browsers and hardware decoders cannot decode; they take 4:2:0 only. I420 is
/// the 4:2:0 format every software encoder takes; hardware encoders, e.g. qtic2venc, take NV12.
pub const DEFAULT_INPUT_FORMAT: &str = "I420";
/// Large enough for a 1080p keyframe at a high bitrate.
pub const DEFAULT_MAX_OUTPUT_SIZE: usize = 2 * 1024 * 1024;
/// The longest one frame may take to encode, unless told otherwise.
pub const DEFAULT_TIMEOUT_MS: u64 = 1000;

/// What the encoder is told to do.
#[derive(clap::Args)]
pub struct Config {
    /// The engine's name.
    #[arg(long, default_value = "h264-encode")]
    pub name: String,
    /// The decoder engine, whose events say which frames were lost. Without one, the encoder
    /// makes a keyframe only at the start, and when one of its own frames is discarded.
    #[arg(long, default_value = "h264-decode")]
    pub decoder: Option<String>,
    /// The H.264 encoder, in gst-launch syntax.
    #[arg(long, default_value = DEFAULT_ENCODER)]
    pub encoder: String,
    /// The 4:2:0 pixel format the encoder takes, e.g. I420 or NV12.
    #[arg(long, default_value = DEFAULT_INPUT_FORMAT)]
    pub input_format: String,
    /// The frame rate the encoder's rate control assumes.
    #[arg(long, default_value_t = DEFAULT_FPS)]
    pub fps: u32,
    /// The largest encoded frame, in bytes.
    #[arg(long, default_value_t = DEFAULT_MAX_OUTPUT_SIZE)]
    pub max_output_size: usize,
    /// The longest one frame may take to encode, in ms.
    #[arg(long, default_value_t = DEFAULT_TIMEOUT_MS)]
    pub timeout_ms: u64,
    /// Print a line per frame.
    #[arg(long)]
    pub log_frames: bool,
}

/// Decides when the next frame of a stream must be a keyframe: after a frame the decoder will not
/// get, which breaks the chain of references from the last keyframe.
struct KeyframePolicy {
    needed: bool,
    /// The input the last keyframe was encoded from.
    keyframe: Option<InputId>,
}

impl KeyframePolicy {
    /// The first frame is a keyframe.
    fn new() -> Self {
        Self {
            needed: true,
            keyframe: None,
        }
    }

    /// A frame was emitted for the input `id`.
    fn emitted(&mut self, id: InputId, keyframe: bool) {
        if keyframe {
            self.needed = false;
            self.keyframe = Some(id);
        }
    }

    /// An event of the decoder's about an input of this stream.
    fn decoder_event(&mut self, event: &Published<'_>) {
        match event.kind() {
            // A frame the decoder will not get.
            EventKind::Skipped | EventKind::TimedOut => self.require_if_since_keyframe(event.id()),
            // Anything but a clean decode breaks the chain, and so does a result that is not
            // one of the decoder's.
            EventKind::Result
                if !DecodeResult::parse_from_bytes(event.result())
                    .is_ok_and(|result| result.decoded && !result.keyframe_needed) =>
            {
                self.require_if_since_keyframe(event.id());
            }
            _ => {}
        }
    }

    /// Requires a keyframe if `id` is the last keyframe's input or a later one. Frames from
    /// before the last keyframe no longer matter.
    fn require_if_since_keyframe(&mut self, id: &InputId) {
        let since = match &self.keyframe {
            Some(keyframe) => id.session() != keyframe.session() || id.seq() >= keyframe.seq(),
            None => true,
        };
        self.needed |= since;
    }
}

/// Encodes frames of one size; a frame of another size starts a new pipeline.
struct Encoder<'a> {
    args: &'a Config,
    start: Instant,
    pipeline: Option<(Pipeline, VideoInfo)>,
}

/// The H.264 stream of one producer.
struct Stream<'a> {
    encoder: Encoder<'a>,
    keyframes: KeyframePolicy,
    /// The number of the next frame encoded, which the decoder uses to see a frame was lost.
    next_number: u64,
}

impl<'a> Stream<'a> {
    fn new(args: &'a Config) -> Self {
        Self {
            encoder: Encoder::new(args),
            keyframes: KeyframePolicy::new(),
            next_number: 0,
        }
    }
}

/// Passes the decoder's `event` to the stream it is about. Events that cannot be tied to a stream
/// mean frames of any stream may be lost.
fn route_decoder_event(streams: &mut HashMap<String, Stream<'_>>, event: &Event<'_>) {
    match event {
        // Frames in flight may have been lost among the events missed.
        Event::Lost(_) => require_keyframes(streams),
        // Frames in flight are lost with the connection.
        Event::Published(event) if event.kind() == EventKind::Disconnected => {
            require_keyframes(streams);
        }
        Event::Published(event) => {
            if let Some(stream) = streams.get_mut(event.id().producer()) {
                stream.keyframes.decoder_event(event);
            }
        }
    }
}

fn require_keyframes(streams: &mut HashMap<String, Stream<'_>>) {
    for stream in streams.values_mut() {
        stream.keyframes.needed = true;
    }
}

impl<'a> Encoder<'a> {
    fn new(args: &'a Config) -> Self {
        Self {
            args,
            start: Instant::now(),
            pipeline: None,
        }
    }

    /// Encodes the RGB `pixels` of a `width` x `height` frame. Returns the encoded frame, or
    /// None if none came in time, and whether outputs of earlier frames were dropped.
    fn encode(
        &mut self,
        pixels: Vec<u8>,
        width: u32,
        height: u32,
        force_keyframe: bool,
    ) -> Result<(Option<gst::Sample>, bool)> {
        let size_changed = self
            .pipeline
            .as_ref()
            .is_none_or(|(_, info)| info.width() != width || info.height() != height);
        if size_changed {
            // The old pipeline stops before the new one starts.
            self.pipeline = None;
            let pipeline = Pipeline::new(
                &format!(
                    "appsrc name=src ! videoconvert ! video/x-raw,format={} ! {} \
                     ! h264parse config-interval=-1 \
                     ! video/x-h264,stream-format=byte-stream,alignment=au ! appsink name=sink",
                    self.args.input_format, self.args.encoder
                ),
                &format!(
                    "video/x-raw,format=RGB,width={width},height={height},framerate={}/1",
                    self.args.fps
                ),
            )?;
            let info = VideoInfo::builder(VideoFormat::Rgb, width, height).build()?;
            self.pipeline = Some((pipeline, info));
        }
        let (pipeline, info) = self.pipeline.as_ref().expect("made above");
        if force_keyframe {
            pipeline.force_keyframe();
        }
        let pts = gst::ClockTime::from_nseconds(self.start.elapsed().as_nanos() as u64);
        pipeline.push(rgb_buffer(pixels, info), pts)?;
        Ok(pipeline.pull(pts, Duration::from_millis(self.args.timeout_ms)))
    }
}

/// A buffer of packed RGB `pixels`, laid out as GStreamer lays out `info`: each row rounded up to
/// 4 bytes. Packed rows already are when the width is a multiple of 4, and are used as they are.
fn rgb_buffer(pixels: Vec<u8>, info: &VideoInfo) -> gst::Buffer {
    let row_bytes = info.width() as usize * RGB_CHANNELS as usize;
    let stride = info.stride()[0] as usize;
    if stride == row_bytes {
        return gst::Buffer::from_mut_slice(pixels);
    }
    let mut strided = vec![0; info.size()];
    for (row, packed) in strided.chunks_mut(stride).zip(pixels.chunks(row_bytes)) {
        row[..row_bytes].copy_from_slice(packed);
    }
    gst::Buffer::from_mut_slice(strided)
}

/// Why `frame` cannot be encoded, if it cannot.
fn invalid(frame: &RawFrameView) -> Option<&'static str> {
    if frame.channels != RGB_CHANNELS {
        Some("it is not 3-channel RGB")
    } else if frame.h_res == 0
        || frame.v_res == 0
        || !frame.h_res.is_multiple_of(2)
        || !frame.v_res.is_multiple_of(2)
    {
        Some("its width and height must be even and positive")
    } else if u32::try_from(frame.h_res).is_err() || u32::try_from(frame.v_res).is_err() {
        Some("it is too large")
    } else if frame.data.len() as u64 != frame.h_res * frame.v_res * RGB_CHANNELS {
        Some("its data does not match its size")
    } else {
        None
    }
}

/// Runs the encoder until `stop` is set.
pub fn run(args: &Config, stop: &AtomicBool) -> Result<()> {
    gst::init()?;

    let mut engine = Engine::new(&args.name, None, args.max_output_size)
        .with_context(|| format!("failed to create engine {}", args.name))?;
    match &args.decoder {
        Some(decoder) => {
            engine.subscribe(decoder)?;
            eprintln!("engine {} ready; watching {decoder}", args.name);
        }
        None => eprintln!("engine {} ready; there is no decoder to watch", args.name),
    }

    let mut streams: HashMap<String, Stream<'_>> = HashMap::new();
    while !stopped(stop) {
        match engine.wait(WAIT_TIMEOUT) {
            Ok(()) | Err(uriel::Error::Again) => {}
            Err(e) => return Err(e.into()),
        }
        loop {
            match engine.poll_event() {
                Ok(event) => route_decoder_event(&mut streams, &event),
                Err(uriel::Error::Again) => break,
                Err(e) => return Err(e.into()),
            }
        }
        let (id, is_raw_frame, mut frame) = match engine.poll_input() {
            Ok(input) => (
                *input.id(),
                input.payload_type() == RAW_FRAME_TYPE,
                RawFrameView::parse_from_bytes(input.data),
            ),
            Err(uriel::Error::Again) => continue,
            Err(e) => return Err(e.into()),
        };
        let started = Instant::now();
        let problem = match &frame {
            _ if !is_raw_frame => Some("its payload type is not a RawFrame"),
            Ok(frame) => invalid(frame),
            Err(_) => Some("it is not a RawFrame"),
        };
        if let Some(problem) = problem {
            eprintln!("discarding input {}: {problem}", id.seq());
            engine.drop(&[])?;
            continue;
        }
        let frame = frame.as_mut().expect("checked above");

        let stream = streams
            .entry(id.producer().to_owned())
            .or_insert_with(|| Stream::new(args));
        let forced = stream.keyframes.needed;
        let pixels = std::mem::take(&mut frame.data);
        let encoding = Instant::now();
        let (sample, dropped_late) =
            stream
                .encoder
                .encode(pixels, frame.h_res as u32, frame.v_res as u32, forced)?;
        let encode_ms = encoding.elapsed().as_secs_f64() * 1e3;
        // Every frame pushed is one the encoder may refer to later.
        let number = stream.next_number;
        stream.next_number += 1;
        if dropped_late {
            // An earlier frame came out too late to send, so the decoder misses it.
            stream.keyframes.needed = true;
        }
        let Some(sample) = sample else {
            eprintln!(
                "frame {number} of {} took over {} ms to encode; discarding it",
                id.producer(),
                args.timeout_ms
            );
            stream.keyframes.needed = true;
            engine.drop(&[])?;
            continue;
        };

        let buffer = sample.buffer().context("an encoded sample has a buffer")?;
        let keyframe = !buffer.flags().contains(gst::BufferFlags::DELTA_UNIT);
        let mut encoded = EncodedFrameView::new();
        encoded.encoded_data = buffer.map_readable()?.to_vec();
        encoded.id = number;
        encoded.keyframe = keyframe;
        encoded.codec = Codec::CODEC_H264.into();
        // What an encoded frame keeps of the raw one besides its pixels.
        encoded.camera_id = frame.camera_id;
        encoded.timestamp = frame.timestamp.take();
        encoded.gimbal_info = frame.gimbal_info.take();
        encoded.position_info = frame.position_info.take();
        let bytes = encoded.write_to_bytes()?;
        if bytes.len() > args.max_output_size {
            eprintln!(
                "frame {number} of {} is {} bytes, over --max-output-size; discarding it",
                id.producer(),
                bytes.len()
            );
            stream.keyframes.needed = true;
            engine.drop(&[])?;
            continue;
        }
        engine.new_output()?[..bytes.len()].write_copy_of_slice(&bytes);
        engine.replace(ENCODED_FRAME_TYPE, bytes.len(), &[])?;
        stream.keyframes.emitted(id, keyframe);
        if forced && !keyframe {
            eprintln!("the encoder ignored a request for a keyframe");
        }
        if args.log_frames {
            // ms is the whole frame, from taking the input to emitting it; encode_ms is the
            // pipeline's part.
            eprintln!(
                "frame {number} of {} input {} keyframe={keyframe} bytes={} ms={:.2} encode_ms={encode_ms:.2}",
                id.producer(),
                id.seq(),
                bytes.len(),
                started.elapsed().as_secs_f64() * 1e3
            );
        }
    }
    Ok(())
}
