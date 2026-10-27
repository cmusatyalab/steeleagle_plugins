import argparse
import threading
import time
from pathlib import Path

import uriel
from steeleagle_protocol.v1.messages.telemetry import telemetry_pb2
from common import FLOW, MAX_FRAME_SIZE, PRODUCER, REMOTE_ADDR, SERVER

WATCHED_ENGINES = {"patchcore-score", "verifier"}
RAW_FRAME_TYPE = telemetry_pb2.RawFrame.DESCRIPTOR.full_name
FRAME_W, FRAME_H = 1280, 720
CAMERA_FPS = 30.0


class CameraFeed:
    """Simulates the drone's camera: a background thread that advances through
    the frame folder at CAMERA_FPS, always holding only the single latest
    frame and a version counter so a reader can tell when it's new."""

    def __init__(self, frames):
        self._frames = frames
        self._lock = threading.Lock()
        self._data = None
        self._version = 0
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        period = 1.0 / CAMERA_FPS
        for path in self._frames:
            if self._stop.is_set():
                return
            data = path.read_bytes()
            with self._lock:
                self._data = data
                self._version += 1
            time.sleep(period)

    def latest(self):
        with self._lock:
            return self._data, self._version


def build_raw_frame(data: bytes, seq: int) -> bytes:
    frame = telemetry_pb2.RawFrame()
    frame.data = data
    frame.h_res = FRAME_W
    frame.v_res = FRAME_H
    frame.pixel_format = telemetry_pb2.RawFrame.PIXEL_FORMAT_YUV420
    frame.id = seq
    return frame.SerializeToString()


def log_events(events, stop: threading.Event):
    while not stop.is_set():
        if events.wait(0.5):
            while (event := events.poll()) is not None:
                if event.engine not in WATCHED_ENGINES:
                    continue
                result_len = len(event.result) if event.result else 0
                preview = bytes(event.result)[:60] if event.result else None
                print(
                    f"    event: engine={event.engine} kind={event.kind} "
                    f"seq={event.id.seq} outcome={getattr(event, 'outcome', None)} "
                    f"result_bytes={result_len} preview={preview!r}",
                    flush=True,
                )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    frames = sorted(Path(args.frames_dir).glob("*.yuv"))
    if args.limit:
        frames = frames[: args.limit]
    print(f"found {len(frames)} frames, streaming at {CAMERA_FPS} fps", flush=True)

    spec = Path("flow_local.toml").read_text()
    cert = Path("certificate.der").read_bytes()
    remote = uriel.RemoteServer(addr=REMOTE_ADDR, certificate=cert, crossing_capacity=8)

    camera = CameraFeed(frames)

    with (
        uriel.Producer(PRODUCER, input_type=RAW_FRAME_TYPE, max_input_size=MAX_FRAME_SIZE) as producer,
        uriel.Subscription.producer(PRODUCER) as events,
        uriel.Client("patchcore-local-client", SERVER, remote=remote) as client,
    ):
        client.add_producer(PRODUCER)
        client.add_flow(FLOW, spec)

        stop_logging = threading.Event()
        log_thread = threading.Thread(target=log_events, args=(events, stop_logging), daemon=True)
        log_thread.start()

        camera.start()

        last_version = 0
        published = 0
        try:
            while True:
                data, version = camera.latest()
                if data is None or version == last_version:
                    time.sleep(0.001)
                    continue
                last_version = version
                payload = build_raw_frame(data, version)
                seq = producer.send(FLOW, payload)
                published += 1
                print(f"published frame version={version} seq={seq}", flush=True)
                if version >= len(frames):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            camera.stop()
            stop_logging.set()
            print(f"published {published} frames total", flush=True)


if __name__ == "__main__":
    main()
