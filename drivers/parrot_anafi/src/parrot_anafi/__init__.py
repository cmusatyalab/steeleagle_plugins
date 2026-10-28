import os
import json
import grpc
import logging
from concurrent import futures
import olympe
# Protocol imports
import steeleagle_protocol.v1.messages.telemetry.telemetry_pb2 as telemetry_proto
from steeleagle_protocol.v1.services.driver.control_pb2_grpc import add_ControlServiceServicer_to_server
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import add_StreamServiceServicer_to_server
from steeleagle_protocol.v1.services.driver.calibrate_pb2_grpc import add_CalibrateServiceServicer_to_server
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
            "level": "INFO"
        }
    }
})

# Default drone IP, matching anafi4k.plugin.toml (the daemon passes only the
# args set in its config, not the plugin's defaults)
DEFAULT_IP = '192.168.42.1'

class DroneWrapper(olympe.Drone):
    """Drone wrapper to keep track of setpoints.

    This wrapper holds setpoint data to exchange between services.
    Namely the StreamService must have access to ControlService
    setpoints to correctly generate telemetry.
    """
    def __init__(self, ip):
        super().__init__(ip)
        self.setpoint = None
        self.flight_mode = telemetry_proto.Mode.MODE_UNSPECIFIED

    def mark_setpoint(self, setpoint):
        self.setpoint = setpoint

    def set_flight_mode(self, mode: telemetry_proto.Mode):
        self.flight_mode = mode

def main():
    """Runs the driver with the args and listen socket the daemon gives it.
    """
    args = json.loads(os.getenv('STEELEAGLE_PLUGIN_ARGS') or '{}')
    ip = args.get('ip')
    if ip is None:
        ip = DEFAULT_IP

    listen_address = os.getenv('STEELEAGLE_LISTEN_SOCKET')
    if not listen_address:
        raise ValueError('no listen socket address provided!')

    # The daemon connects once the socket exists, so the drone is connected
    # first
    logger.info(f'connecting to drone at {ip}')
    drone = DroneWrapper(ip)
    if not drone.connect():
        raise ConnectionError('cannot connect to device!')

    server = grpc.server(
        thread_pool=futures.ThreadPoolExecutor(max_workers=10)
    )
    add_ControlServiceServicer_to_server(Control(drone), server)
    add_StreamServiceServicer_to_server(Stream(drone, ip), server)
    add_CalibrateServiceServicer_to_server(Calibrate(drone), server)
    server.add_insecure_port(f'unix://{listen_address}')
    server.start()
    logger.info('all services started')
    server.wait_for_termination()
