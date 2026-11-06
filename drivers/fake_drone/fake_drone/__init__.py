import atexit
import json
import logging
import os
import subprocess
import sys
from concurrent import futures
from pathlib import Path
import signal
import grpc
from steeleagle_protocol.v1.services.driver.stream_pb2_grpc import add_StreamServiceServicer_to_server
from fake_drone.capture import DEFAULT_DECODER
from fake_drone.stream import Stream

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fake-drone/main")

DEFAULT_VIDEO_PATH = "/home/particle/uriel/patchcore-pipeline/test.mp4"

NETSIM_PROFILES = {"off", "normal", "limited", "degraded", "intermittent", "denied"}
NETSIM_PORT = 9001

_netsim_proc = None


def _start_netsim(profile, interface, direction, script_path):
    global _netsim_proc
    if profile not in NETSIM_PROFILES:
        raise ValueError(f"netsim_profile must be one of {sorted(NETSIM_PROFILES)}, got {profile!r}")
    if profile == "off":
        return
    if not interface:
        raise ValueError("netsim_interface is required when netsim_profile is not 'off'")
    if not Path(script_path).is_file():
        raise ValueError(f"netsim_script {script_path!r} does not exist")
    _netsim_proc = subprocess.Popen([
        sys.executable, script_path,
        "-i", interface, "-p", str(NETSIM_PORT),
        "--direction", direction, "--profile", profile,
    ])
    logger.info(f"netsim started (pid {_netsim_proc.pid}): profile={profile} egress on {interface} port {NETSIM_PORT}")


def _stop_netsim():
    if _netsim_proc is not None and _netsim_proc.poll() is None:
        _netsim_proc.terminate()
        try:
            _netsim_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _netsim_proc.kill()
            _netsim_proc.wait()
        logger.info("netsim stopped")


atexit.register(_stop_netsim)


def main():
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda s, _f: sys.exit(128 + s))

    args = json.loads(os.getenv("STEELEAGLE_PLUGIN_ARGS") or "{}")
    video_path = args.get("video_path") or DEFAULT_VIDEO_PATH
    decoder = args.get("decoder") or DEFAULT_DECODER
    netsim_profile = args.get("netsim_profile") or "off"
    netsim_interface = args.get("netsim_interface") or ""
    netsim_direction = args.get("netsim_direction") or "egress"
    netsim_script = args.get("netsim_script") or "/root/steeleagle/tools/netsim/netsim.py"
    listen_address = os.getenv("STEELEAGLE_LISTEN_SOCKET")
    if not listen_address:
        raise ValueError("no listen socket address provided!")
    Path(listen_address).unlink(missing_ok=True)

    _start_netsim(netsim_profile, netsim_interface, netsim_direction)

    logger.info(f"reading from {video_path}, decoding with {decoder!r}")
    server = grpc.server(thread_pool=futures.ThreadPoolExecutor(max_workers=10))
    add_StreamServiceServicer_to_server(Stream(video_path, decoder), server)
    server.add_insecure_port(f"unix://{listen_address}")
    server.start()
    logger.info("all services started")
    server.wait_for_termination()


if __name__ == "__main__":
    main()
