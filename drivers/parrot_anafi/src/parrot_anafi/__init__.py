import os
import grpc
import logging
import argparse
from concurrent import futures
import olympe
# Protocol imports
from steeleagle_protocol.v1.services.driver.control_pb2_grpc import ControlServiceServicer, add_ControlServiceServicer_to_server
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import StreamServiceServicer, add_StreamServiceServicer_to_server
from steeleagle_protocol.v1.services.driver.calibrate_pb2_grpc import CalibrateServiceServicer, add_CalibrateServiceServicer_to_server
# Service imports
from parrot_anafi.control import Control
from parrot_anafi.stream import Stream
from parrot_anafi.calibrate import Calibrate

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("parrot-anafi/main")

olympe.log.update_config({
    "loggers": {
        "olympe": {
            "level": "DEBUG"
        }
    }
})

def main():
    """Runs the driver.
    """
    parser = argparse.ArgumentParser(
        description='Connects to a Parrot Anafi/Anafi USA drone.'
    )
    parser.add_argument(
        '--ip',
        type=str,
        default='192.168.42.1',
        help='ip address to connect to the drone'
    )
    args = parser.parse_args()

    drone = olympe.Drone(args.ip)
    if not drone.connect():
        raise ConnectionError('cannot connect to device!')

    listen_address = os.getenv('LISTEN_SOCKET')
    if not listen_address:
        raise ValueError('no listen socket address provided!')
    os.makedirs(os.path.dirname(listen_address), exist_ok=True)

    server = grpc.server(
        thread_pool=futures.ThreadPoolExecutor(max_workers=10)
    )
    add_ControlServiceServicer_to_server(Control(drone), server)
    add_StreamServiceServicer_to_server(Stream(drone, args.ip), server)
    add_CalibrateServiceServicer_to_server(Calibrate(drone), server)
    server.add_insecure_port(f'unix://{listen_address}')
    server.start()
    logger.info('all services started')
    server.wait_for_termination()
