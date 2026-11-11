# Rescale

SteelEagle engine plugin that rescales camera frames (`RawFrame`) to a target resolution and aspect ratio and sends them on. Frames are stretched to the aspect ratio, not cropped or padded. A frame is resized with OpenCV's area interpolation, keeping its id, timestamp and pose, and replaces its input, which keeps the input's id. A frame already at the resolution is sent on unchanged; a frame whose pixels don't fit its resolution is dropped.

| Arg | Default | Description |
| --- | --- | --- |
| `resolution` | | Required: `4k`, `1080p`, `720p`, `480p`, `360p`, `240p` or `180p`, which sets the height (2160 for `4k`) |
| `aspect_ratio` | `16:9` | `W:H`; the width is the height times W/H, rounded to an even number of pixels |

| Resolution | 16:9 | 4:3 | 1:1 |
| --- | --- | --- | --- |
| `4k` | 3840x2160 | 2880x2160 | 2160x2160 |
| `1080p` | 1920x1080 | 1440x1080 | 1080x1080 |
| `720p` | 1280x720 | 960x720 | 720x720 |
| `480p` | 854x480 | 640x480 | 480x480 |
| `360p` | 640x360 | 480x360 | 360x360 |
| `240p` | 426x240 | 320x240 | 240x240 |
| `180p` | 320x180 | 240x180 | 180x180 |

## Development

```
pixi run -e test test   # all tests, including one through a real Uriel server
```
