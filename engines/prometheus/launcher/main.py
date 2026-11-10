"""Runs Prometheus as an eagled service.

eagled starts a service with `pixi run start`, its args in
STEELEAGLE_PLUGIN_ARGS, and waits for it to create the socket named in
STEELEAGLE_LISTEN_SOCKET, which it then routes the service's gRPC calls to.
Prometheus creates no such socket, so the launcher does: it serves the
standard gRPC health service there, SERVING while Prometheus is ready. The
launcher exits when Prometheus does, with its status, so eagled sees the
service stop; on SIGTERM or SIGINT it stops Prometheus first.
"""
import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent import futures
from pathlib import Path

import grpc
import yaml
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from launcher.config import parse_args, ready_url, render_config

ARGS_ENV = "STEELEAGLE_PLUGIN_ARGS"
SOCKET_ENV = "STEELEAGLE_LISTEN_SOCKET"
DEFAULT_PROMETHEUS = Path(__file__).resolve().parent.parent / "bin" / "prometheus"
CONFIG_FILE = "prometheus.yml"
# The health of the whole service, as the gRPC health protocol names it.
SERVICE = ""
READY_POLL_INTERVAL_S = 1.0
READY_TIMEOUT_S = 1.0
SERVER_WORKERS = 2
SERVER_GRACE_S = 1.0
# How long Prometheus may take to stop on SIGTERM before it is killed.
STOP_TIMEOUT_S = 30.0
HTTP_OK = 200

logger = logging.getLogger("prometheus-launcher")


def _ready(url):
    try:
        with urllib.request.urlopen(url, timeout=READY_TIMEOUT_S) as response:
            return response.status == HTTP_OK
    except (urllib.error.URLError, OSError):
        return False


def run(args, prometheus, socket_path, run_dir, stop=None):
    """Runs Prometheus, the command `prometheus` (a list), with `args`, its
    config and database in `run_dir`, serving gRPC health on the unix socket
    `socket_path`, until Prometheus exits or `stop` is set. Returns
    Prometheus's exit code."""
    stop = stop or threading.Event()
    run_dir = Path(run_dir)
    config_path = run_dir / CONFIG_FILE
    config_path.write_text(yaml.safe_dump(render_config(args)))
    storage = run_dir / args.storage_dir

    child = subprocess.Popen(
        [
            *prometheus,
            f"--config.file={config_path}",
            f"--web.listen-address={args.listen_address}",
            f"--storage.tsdb.path={storage}",
            f"--storage.tsdb.retention.time={args.retention}",
        ]
    )
    servicer = health.HealthServicer()
    servicer.set(SERVICE, health_pb2.HealthCheckResponse.NOT_SERVING)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=SERVER_WORKERS))
    health_pb2_grpc.add_HealthServicer_to_server(servicer, server)
    server.add_insecure_port(f"unix:{socket_path}")
    server.start()
    logger.info("serving health on %s; waiting for Prometheus at %s", socket_path, ready_url(args))

    url = ready_url(args)
    try:
        while child.poll() is None and not stop.is_set():
            ready = _ready(url)
            servicer.set(
                SERVICE,
                health_pb2.HealthCheckResponse.SERVING if ready else health_pb2.HealthCheckResponse.NOT_SERVING,
            )
            stop.wait(READY_POLL_INTERVAL_S)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    finally:
        server.stop(SERVER_GRACE_S).wait()
        Path(socket_path).unlink(missing_ok=True)
    logger.info("Prometheus exited with %d", child.returncode)
    return child.returncode


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prometheus", default=str(DEFAULT_PROMETHEUS), help="Prometheus server executable")
    options = parser.parse_args(argv)
    socket_path = os.environ.get(SOCKET_ENV)
    if not socket_path:
        sys.exit(f"{SOCKET_ENV} is not set")

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    code = run(parse_args(os.environ.get(ARGS_ENV)), [options.prometheus], socket_path, Path.cwd(), stop)
    sys.exit(code)


if __name__ == "__main__":
    main()
