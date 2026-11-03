"""Entrypoint of the fake-drone plugin, which eagled starts with `pixi run
start` (console script fake_drone:main).

eagled passes this driver's args in STEELEAGLE_PLUGIN_ARGS (a JSON object)
and the unix socket to serve StreamService on in STEELEAGLE_LISTEN_SOCKET.
"""
import json
import logging
import os
from concurrent import futures
from pathlib import Path

import grpc

from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import add_StreamServiceServicer_to_server

from fake_drone.stream import Stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fake-drone/main")

DEFAULT_FRAMES_DIR = "/home/particle/uriel/patchcore-pipeline/test_rgb_200"
DEFAULT_FPS = 30
DEFAULT_H_RES = 1280
DEFAULT_V_RES = 720


def main():
    """Runs the driver with the args and listen socket the daemon gives it."""
    args = json.loads(os.getenv("STEELEAGLE_PLUGIN_ARGS") or "{}")
    frames_dir = args.get("frames_dir") or DEFAULT_FRAMES_DIR
    fps = args.get("fps") or DEFAULT_FPS

    listen_address = os.getenv("STEELEAGLE_LISTEN_SOCKET")
    if not listen_address:
        raise ValueError("no listen socket address provided!")
    # Remove a socket left over from a previous run, so the daemon doesn't
    # connect to it before this one is listening
    Path(listen_address).unlink(missing_ok=True)

    logger.info(f"reading frames from {frames_dir} at {fps} fps")

    server = grpc.server(thread_pool=futures.ThreadPoolExecutor(max_workers=10))
    add_StreamServiceServicer_to_server(Stream(frames_dir, fps, DEFAULT_H_RES, DEFAULT_V_RES), server)
    server.add_insecure_port(f"unix://{listen_address}")
    server.start()
    logger.info("all services started")
    server.wait_for_termination()
