//! The decoder engine: decodes the SteelEagle `EncodedFrame`s of the encoder, and emits each as the
//! raw RGB frame it was encoded from, of the same payload type, e.g. a SteelEagle `RawFrame` for a
//! detector.
//!
//! A P-frame decodes correctly only after every frame before it, back to a keyframe. After a gap
//! in the frame numbers, the decoder drops frames until the next keyframe, and says in its result
//! that it needs one; the encoder, which subscribes to the decoder's events, then makes one.
//!
//! Each producer's frames are a stream of their own, with a pipeline and frame numbers that the
//! encoder keeps apart, so the decoder does too.

use std::{
    collections::{HashMap, hash_map::Entry},
    mem::MaybeUninit,
    sync::atomic::AtomicBool,
    time::{Duration, Instant},
};

use crate::{
    Codec, DecodeResult, ENCODED_FRAME_TYPE, EncodedFrameView, Pipeline, RAW_FRAME_DATA_FIELD,
    RAW_FRAME_TYPE, RGB_CHANNELS, RawFrameView, WAIT_TIMEOUT, stopped, type_name,
};
use anyhow::{Context, Result};
use gstreamer::{self as gst};
use gstreamer_video::VideoInfo;
use protobuf::{CodedOutputStream, Message, rt::WireType};
use uriel::Engine;

/// Software libav, on one thread: frame threading holds frames back.
pub const DEFAULT_DECODER: &str = "avdec_h264 max-threads=1";
/// Room for the fields of a RawFrame other than its pixels.
const RAW_FRAME_OVERHEAD: usize = 64 * 1024;
/// Timestamps only match inputs to outputs, so any step keeps them apart.
const PTS_STEP: gst::ClockTime = gst::ClockTime::MSECOND;

/// The widest frame, unless told otherwise.
pub const DEFAULT_MAX_WIDTH: usize = 1920;
/// The tallest frame, unless told otherwise.
pub const DEFAULT_MAX_HEIGHT: usize = 1080;
/// The longest one frame may take to decode, unless told otherwise.
pub const DEFAULT_TIMEOUT_MS: u64 = 1000;

/// What the decoder is told to do.
#[derive(clap::Args)]
pub struct Config {
    /// The engine's name.
    #[arg(long, default_value = "h264-decode")]
    pub name: String,
    /// The H.264 decoder, in gst-launch syntax.
    #[arg(long, default_value = DEFAULT_DECODER)]
    pub decoder: String,
    /// The widest frame, in pixels.
    #[arg(long, default_value_t = DEFAULT_MAX_WIDTH)]
    pub max_width: usize,
    /// The tallest frame, in pixels.
    #[arg(long, default_value_t = DEFAULT_MAX_HEIGHT)]
    pub max_height: usize,
    /// The longest one frame may take to decode, in ms.
    #[arg(long, default_value_t = DEFAULT_TIMEOUT_MS)]
    pub timeout_ms: u64,
    /// Print a line per frame.
    #[arg(long)]
    pub log_frames: bool,
}

impl Config {
    /// The largest frame the decoder emits, in bytes.
    fn max_output_size(&self) -> usize {
        self.max_width * self.max_height * RGB_CHANNELS as usize + RAW_FRAME_OVERHEAD
    }
}

/// The H.264 stream of one producer.
struct Stream {
    pipeline: Pipeline,
    /// The number the next frame must have to decode, once a keyframe started the chain.
    expected: Option<u64>,
}

impl Stream {
    fn new(decoder: &str) -> Result<Self> {
        let pipeline = Pipeline::new(
            &format!(
                "appsrc name=src ! h264parse ! {decoder} ! videoconvert ! video/x-raw,format=RGB \
                 ! appsink name=sink"
            ),
            "video/x-h264,stream-format=byte-stream,alignment=au",
        )?;
        Ok(Self {
            pipeline,
            expected: None,
        })
    }
}

/// What became of an encoded frame, as its result says.
fn result(decoded: bool, keyframe_needed: bool) -> Result<Vec<u8>> {
    let mut result = DecodeResult::new();
    result.decoded = decoded;
    result.keyframe_needed = keyframe_needed;
    Ok(result.write_to_bytes()?)
}

/// Writes a RawFrame to `out`: `fields`, its fields other than its pixels, then the pixels of
/// `sample`, packed. A message's fields may come in any order, so the pixels go last, copied
/// once, straight from the decoder's buffer. Returns its size, or None if it does not fit.
fn write_raw_frame(
    fields: &[u8],
    sample: &gst::Sample,
    out: &mut [MaybeUninit<u8>],
) -> Result<Option<usize>> {
    let caps = sample.caps().context("the decoder's output has no caps")?;
    let info = VideoInfo::from_caps(caps)?;
    let row_bytes = info.width() as usize * RGB_CHANNELS as usize;
    let stride = info.stride()[0] as usize;
    let pixel_bytes = row_bytes * info.height() as usize;

    let mut header = Vec::new();
    let mut stream = CodedOutputStream::vec(&mut header);
    stream.write_tag(RAW_FRAME_DATA_FIELD, WireType::LengthDelimited)?;
    stream.write_raw_varint64(pixel_bytes as u64)?;
    stream.flush()?;
    drop(stream);

    let size = fields.len() + header.len() + pixel_bytes;
    if size > out.len() {
        return Ok(None);
    }
    let (out_fields, rest) = out.split_at_mut(fields.len());
    out_fields.write_copy_of_slice(fields);
    let (out_header, out_pixels) = rest.split_at_mut(header.len());
    out_header.write_copy_of_slice(&header);
    let buffer = sample.buffer().context("a decoded sample has a buffer")?;
    let pixels = buffer.map_readable()?;
    for (out_row, row) in out_pixels[..pixel_bytes]
        .chunks_mut(row_bytes)
        .zip(pixels.chunks(stride))
    {
        out_row.write_copy_of_slice(&row[..row_bytes]);
    }
    Ok(Some(size))
}

/// Runs the decoder until `stop` is set.
pub fn run(args: &Config, stop: &AtomicBool) -> Result<()> {
    gst::init()?;
    let decoded = result(true, false)?;
    let dropped = result(false, false)?;
    let keyframe_needed = result(false, true)?;

    let mut engine = Engine::new(
        &args.name,
        Some(&type_name::<DecodeResult>()),
        args.max_output_size(),
    )
    .with_context(|| format!("failed to create engine {}", args.name))?;
    eprintln!("engine {} ready", args.name);

    let mut streams: HashMap<String, Stream> = HashMap::new();
    while !stopped(stop) {
        match engine.wait(WAIT_TIMEOUT) {
            Ok(()) | Err(uriel::Error::Again) => {}
            Err(e) => return Err(e.into()),
        }
        let (producer, seq, encoded) = match engine.poll_input() {
            Ok(input) => (
                input.id().producer().to_owned(),
                input.id().seq(),
                (input.payload_type() == ENCODED_FRAME_TYPE)
                    .then(|| EncodedFrameView::parse_from_bytes(input.data).ok())
                    .flatten()
                    .filter(|encoded| encoded.codec == Codec::CODEC_H264.into()),
            ),
            Err(uriel::Error::Again) => continue,
            Err(e) => return Err(e.into()),
        };
        let started = Instant::now();
        let Some(mut encoded) = encoded else {
            eprintln!("discarding input {seq}: not an {ENCODED_FRAME_TYPE} of H.264");
            engine.drop(&dropped)?;
            continue;
        };
        let number = encoded.id;
        let stream = match streams.entry(producer.clone()) {
            Entry::Occupied(stream) => stream.into_mut(),
            Entry::Vacant(slot) => slot.insert(Stream::new(&args.decoder)?),
        };
        if !encoded.keyframe && stream.expected != Some(number) {
            // A frame before this one was lost; until a keyframe, nothing decodes.
            stream.expected = None;
            if args.log_frames {
                eprintln!(
                    "frame {number} of {producer} input {seq} follows a lost frame; waiting for a keyframe"
                );
            }
            engine.drop(&keyframe_needed)?;
            continue;
        }
        // The decoder keeps this frame as a reference whether or not its output comes in time,
        // so the next one decodes either way.
        stream.expected = Some(number + 1);

        let pts = PTS_STEP * number;
        stream.pipeline.push(
            gst::Buffer::from_mut_slice(std::mem::take(&mut encoded.encoded_data)),
            pts,
        )?;
        let (sample, _) = stream
            .pipeline
            .pull(pts, Duration::from_millis(args.timeout_ms));
        let Some(sample) = sample else {
            eprintln!(
                "frame {number} of {producer} took over {} ms to decode; discarding it",
                args.timeout_ms
            );
            engine.drop(&dropped)?;
            continue;
        };
        let info = VideoInfo::from_caps(sample.caps().context("decoded sample has caps")?)?;

        // The raw frame, but for its pixels.
        let mut raw_frame = RawFrameView::new();
        raw_frame.h_res = u64::from(info.width());
        raw_frame.v_res = u64::from(info.height());
        raw_frame.channels = RGB_CHANNELS;
        raw_frame.id = number;
        raw_frame.camera_id = encoded.camera_id;
        raw_frame.timestamp = encoded.timestamp.take();
        raw_frame.gimbal_info = encoded.gimbal_info.take();
        raw_frame.position_info = encoded.position_info.take();
        let fields = raw_frame.write_to_bytes()?;

        let Some(size) = write_raw_frame(&fields, &sample, engine.new_output()?)? else {
            eprintln!(
                "frame {number} of {producer} is larger than --max-width x --max-height; discarding it"
            );
            engine.drop(&dropped)?;
            continue;
        };
        engine.replace(RAW_FRAME_TYPE, size, &decoded)?;
        if args.log_frames {
            eprintln!(
                "frame {number} of {producer} input {seq} keyframe={} ms={:.2}",
                encoded.keyframe,
                started.elapsed().as_secs_f64() * 1e3
            );
        }
    }
    Ok(())
}
