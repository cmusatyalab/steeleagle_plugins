import json
import logging
import os
from concurrent import futures
from pathlib import Path

import grpc

from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import add_StreamServiceServicer_to_server

from fake_drone.capture import DEFAULT_DECODER
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


def find_video(video):
    """The path of the video arg: the file of that name in the daemon's files
    directory (STEELEAGLE_FILES), if there is one, or else the arg as a
    path."""
    files = os.getenv('STEELEAGLE_FILES')
    if files and (Path(files) / video).is_file():
        return str(Path(files) / video)
    if Path(video).is_file():
        return video
    where = f' in STEELEAGLE_FILES ({files})' if files else ' (STEELEAGLE_FILES is not set)'
    raise FileNotFoundError(f'video {video!r} is not a file{where} or as a path')


def main():
    """Runs the driver with the args and listen socket the daemon gives it.
    The daemon passes only the args set in its config, not the plugin's
    defaults, so they are defaulted here too."""
    args = json.loads(os.getenv('STEELEAGLE_PLUGIN_ARGS') or '{}')
    # video_path is the arg's old name, still read for older configs
    video = args.get('video') or args.get('video_path')
    if not video:
        raise ValueError('no video given!')
    video_path = find_video(video)
    decoder = args.get('decoder') or DEFAULT_DECODER
    loop = not _flag(args.get('no_loop', False))

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

    logger.info(f'replaying {video_path} {"in a loop" if loop else "once"}, '
                f'decoding with {decoder!r}, '
                f'{"with" if telemetry is not None else "without"} telemetry')
    server = grpc.server(
        thread_pool=futures.ThreadPoolExecutor(max_workers=10)
    )

    def on_end():
        # With no_loop, the plugin stops once the video has played
        logger.info('video finished; stopping the plugin')
        server.stop(grace=1)

    stream = Stream(video_path, decoder, telemetry, loop=loop, on_end=on_end)
    add_StreamServiceServicer_to_server(stream, server)
    server.add_insecure_port(f'unix://{listen_address}')
    server.start()
    logger.info('all services started')
    server.wait_for_termination()


if __name__ == '__main__':
    main()
