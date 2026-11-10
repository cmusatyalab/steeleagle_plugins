import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import grpc
import yaml
from grpc_health.v1 import health_pb2, health_pb2_grpc

from launcher.config import parse_args, render_config
from launcher.main import run

FAKE = Path(__file__).with_name("fake_prometheus.py")
PACKAGE_DIR = Path(__file__).parent.parent
SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING
# Far longer than anything here takes, so a test fails rather than hangs.
WAIT_S = 5
POLL_S = 0.1


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_executable(tmp_path):
    """An executable that runs the fake server, as the launcher runs Prometheus."""
    path = tmp_path / "fake-prometheus"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    path.chmod(0o755)
    return path


def args_for(port):
    return parse_args(f'{{"listen_address": "127.0.0.1:{port}"}}')


def health(socket_path):
    with grpc.insecure_channel(f"unix:{socket_path}") as channel:
        stub = health_pb2_grpc.HealthStub(channel)
        return stub.Check(health_pb2.HealthCheckRequest(service=""), timeout=WAIT_S).status


def wait_for(condition):
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(POLL_S)
    return False


def start_run(tmp_path, monkeypatch, **fake_env):
    for name, value in fake_env.items():
        monkeypatch.setenv(name, str(value))
    socket_path = tmp_path / "out.sock"
    stop = threading.Event()
    result = {}
    args = args_for(free_port())

    def target():
        result["code"] = run(args, [str(fake_executable(tmp_path))], str(socket_path), tmp_path, stop)

    thread = threading.Thread(target=target)
    thread.start()
    assert wait_for(socket_path.exists)
    return args, socket_path, stop, thread, result


def test_health_is_not_serving_until_prometheus_is_ready(tmp_path, monkeypatch):
    _, socket_path, stop, thread, _ = start_run(tmp_path, monkeypatch, FAKE_READY_AFTER_S=1)
    try:
        assert health(socket_path) == NOT_SERVING
        assert wait_for(lambda: health(socket_path) == SERVING)
    finally:
        stop.set()
        thread.join(WAIT_S)


def test_launcher_exits_with_prometheus_exit_code(tmp_path, monkeypatch):
    _, socket_path, _, thread, result = start_run(
        tmp_path, monkeypatch, FAKE_EXIT_AFTER_S=0.5, FAKE_EXIT_CODE=3
    )

    thread.join(WAIT_S)

    assert result["code"] == 3
    assert not socket_path.exists()


def test_stop_stops_prometheus(tmp_path, monkeypatch):
    _, _, stop, thread, result = start_run(tmp_path, monkeypatch)

    stop.set()
    thread.join(WAIT_S)

    assert not thread.is_alive()
    assert result["code"] == 0


def test_writes_the_rendered_config(tmp_path, monkeypatch):
    args, _, stop, thread, _ = start_run(tmp_path, monkeypatch)
    stop.set()
    thread.join(WAIT_S)

    assert yaml.safe_load((tmp_path / "prometheus.yml").read_text()) == render_config(args)


def test_sigterm_stops_the_launcher_and_prometheus(tmp_path):
    socket_path = tmp_path / "out.sock"
    env = {
        **os.environ,
        "PYTHONPATH": str(PACKAGE_DIR),
        "STEELEAGLE_PLUGIN_ARGS": f'{{"listen_address": "127.0.0.1:{free_port()}"}}',
        "STEELEAGLE_LISTEN_SOCKET": str(socket_path),
    }
    launcher = subprocess.Popen(
        [sys.executable, "-m", "launcher.main", "--prometheus", str(fake_executable(tmp_path))],
        cwd=tmp_path,
        env=env,
    )
    try:
        assert wait_for(lambda: socket_path.exists() and health(socket_path) == SERVING)

        launcher.send_signal(signal.SIGTERM)

        assert launcher.wait(WAIT_S) == 0
    finally:
        launcher.kill()
