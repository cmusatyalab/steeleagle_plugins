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


def main():
    """Runs the driver with the args and listen socket the daemon gives it.
    The daemon passes only the args set in its config, not the plugin's
    defaults, so they are defaulted here too."""
    args = json.loads(os.getenv('STEELEAGLE_PLUGIN_ARGS') or '{}')
    video_path = args.get('video_path')
    if not video_path:
        raise ValueError('no video_path given!')
    if not Path(video_path).is_file():
        raise FileNotFoundError(f'video_path {video_path!r} is not a file')
    decoder = args.get('decoder') or DEFAULT_DECODER

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

    logger.info(f'replaying {video_path}, decoding with {decoder!r}, '
                f'{"with" if telemetry is not None else "without"} telemetry')
    server = grpc.server(
        thread_pool=futures.ThreadPoolExecutor(max_workers=10)
    )
    add_StreamServiceServicer_to_server(Stream(video_path, decoder, telemetry), server)
    server.add_insecure_port(f'unix://{listen_address}')
    server.start()
    logger.info('all services started')
    server.wait_for_termination()


if __name__ == '__main__':
    main()
