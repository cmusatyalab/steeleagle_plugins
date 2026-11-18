import json
import logging
import os
from concurrent import futures
from pathlib import Path

import grpc

from steeleagle_protocol.v1.services.driver.control_pb2_grpc import add_ControlServiceServicer_to_server
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import add_StreamServiceServicer_to_server

from fake_drone.capture import DEFAULT_DECODER
from fake_drone.control import Control
from fake_drone.images import ImageCapture
from fake_drone.stream import Stream
from fake_drone.vmeta import Telemetry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("fake-drone/main")


def _flag(value):
    """An arg given as a JSON boolean, or as a string such as "true"."""
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def find_video(video, kind='file'):
    """The path of the video arg: the file of that name in the daemon's files
    directory (STEELEAGLE_FILES), if there is one, or else the arg as a
    path. With kind 'dir', a directory is looked for instead (the images
    arg)."""
    exists = Path.is_dir if kind == 'dir' else Path.is_file
    files = os.getenv('STEELEAGLE_FILES')
    if files and exists(Path(files) / video):
        return str(Path(files) / video)
    if exists(Path(video)):
        return video
    where = f' in STEELEAGLE_FILES ({files})' if files else ' (STEELEAGLE_FILES is not set)'
    what = 'a directory' if kind == 'dir' else 'a file'
    raise FileNotFoundError(f'{video!r} is not {what}{where} or as a path')


def main():
    """Runs the driver with the args and listen socket the daemon gives it.
    The daemon passes only the args set in its config, not the plugin's
    defaults, so they are defaulted here too."""
    args = json.loads(os.getenv('STEELEAGLE_PLUGIN_ARGS') or '{}')
    # video_path is the arg's old name, still read for older configs
    video = args.get('video') or args.get('video_path')
    images = args.get('images')
    if bool(video) == bool(images):
        raise ValueError('give exactly one of video and images!')
    decoder = args.get('decoder') or DEFAULT_DECODER
    loop = not _flag(args.get('no_loop', False))
    open_capture = None
    if images:
        video_path = find_video(images, kind='dir')
        fps = float(args.get('fps') or 30)
        max_width = int(args.get('max_width', 1920) or 0)
        if _flag(args.get('telemetry', False)):
            logger.warning('telemetry has no effect with images')
            args['telemetry'] = False
        open_capture = lambda loop: ImageCapture(video_path, fps, loop=loop, max_width=max_width)  # noqa: E731
    else:
        video_path = find_video(video)
    pad_after = _flag(args.get('pad_after', False))
    if pad_after and loop:
        logger.warning('pad_after has no effect without no_loop: a looping video never ends')

    # Read before the socket exists, so that a recording without telemetry
    # is reported before the daemon connects
    telemetry = None
    if _flag(args.get('telemetry', False)):
        try:
            telemetry = Telemetry.read(video_path)
        except Exception as e:
            logger.warning(f'telemetry is on, but none could be read; frames '
                           f'are sent without it: {e}')

    listen_address = os.getenv('STEELEAGLE_LISTEN_SOCKET')
    if not listen_address:
        raise ValueError('no listen socket address provided!')
    # Remove a socket left over from a previous run
    Path(listen_address).unlink(missing_ok=True)

    logger.info(f'replaying {"the images in " if images else ""}{video_path} {"in a loop" if loop else "once"}, '
                f'decoding with {decoder!r}, '
                f'{"with" if telemetry is not None else "without"} telemetry'
                f'{", padding with blank frames after it ends" if pad_after and not loop else ""}')
    server = grpc.server(
        thread_pool=futures.ThreadPoolExecutor(max_workers=10)
    )

    def on_end():
        # With no_loop, the plugin stops once the video has played, unless
        # pad_after keeps it sending blank frames
        logger.info('video finished; stopping the plugin')
        server.stop(grace=1)

    # The frame log is rewritten at each playback, so it holds the last one
    frame_log = None
    if args.get('frame_log'):
        frame_log = Path(args['frame_log'])
        files = os.getenv('STEELEAGLE_FILES')
        if not frame_log.is_absolute() and files:
            frame_log = Path(files) / frame_log
        frame_log.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f'logging each frame published to {frame_log}')

    stream = Stream(video_path, decoder, telemetry, loop=loop, on_end=on_end,
                    pad_after=pad_after, open_capture=open_capture, frame_log=frame_log)
    add_StreamServiceServicer_to_server(stream, server)
    # Commands are accepted and ignored, so missions compile and run (see control.py)
    add_ControlServiceServicer_to_server(Control(), server)
    server.add_insecure_port(f'unix://{listen_address}')
    server.start()
    logger.info('all services started')
    server.wait_for_termination()


if __name__ == '__main__':
    main()
