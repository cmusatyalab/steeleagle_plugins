# Thumbnail

SteelEagle engine plugins that carry frames across the network as still images, each of which stands alone: a frame Uriel skips or times out on the way costs only that frame.

| Plugin | Input | Output |
| --- | --- | --- |
| `jpeg_encoder.plugin.toml` | an RGB `RawFrame` | an `EncodedFrame` (`CODEC_JPEG`), numbered per producer |
| `jpeg_decoder.plugin.toml` | a JPEG `EncodedFrame` | the RGB `RawFrame` it decodes to |
| `webp_encoder.plugin.toml` | an RGB `RawFrame` | an `EncodedFrame` of WebP, keeping the frame's id |
| `webp_decoder.plugin.toml` | a WebP `EncodedFrame` | the RGB `RawFrame` it decodes to |

Every output keeps its frame's timestamp and pose. JPEG goes through GStreamer (`jpegenc`/`jpegdec`); WebP through libwebp (Pillow). WebP is about 40% smaller than JPEG at equal PSNR, which suits small images over a thin link, such as the tiles `cliped` sends.

The protocol's `Codec` enum has no WebP value yet, so WebP frames are sent as `CODEC_UNSPECIFIED`, and the WebP decoder takes a frame for WebP by its RIFF/WEBP header.

## Args

JPEG encoder: `encoder` (`jpegenc quality=85`, gst-launch syntax), `max_output_size`, `timeout_ms` (`1000`), `log_frames`.
JPEG decoder: `decoder` (`jpegdec`), `max_output_size`, `timeout_ms`, `log_frames`.
WebP encoder: `quality` (`60`), `method` (`4`, 0 fastest to 6 smallest), `lossless` (`false`), `max_output_size`, `log_frames`.
WebP decoder: `max_output_size`, `log_frames`.

`max_output_size` defaults to `6237184`, a 1080p RGB frame.

## Development

```
pixi run -e test test   # all tests, including encoder → decoder through a real Uriel server
```
