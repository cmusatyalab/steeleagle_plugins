"""Stands in for the Prometheus server in the launcher's tests: answers
/-/ready on the port of --web.listen-address, once FAKE_READY_AFTER_S has
passed, and exits 0 on SIGTERM, or with FAKE_EXIT_CODE after
FAKE_EXIT_AFTER_S if both are set."""
import http.server
import os
import signal
import sys
import threading
import time

LISTEN_FLAG = "--web.listen-address="
READY_PATH = "/-/ready"
HTTP_OK = 200
HTTP_UNAVAILABLE = 503


def main():
    address = next(arg[len(LISTEN_FLAG):] for arg in sys.argv if arg.startswith(LISTEN_FLAG))
    port = int(address.rpartition(":")[2])
    ready_at = time.monotonic() + float(os.environ.get("FAKE_READY_AFTER_S", "0"))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ready = self.path == READY_PATH and time.monotonic() >= ready_at
            self.send_response(HTTP_OK if ready else HTTP_UNAVAILABLE)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    if "FAKE_EXIT_AFTER_S" in os.environ:
        time.sleep(float(os.environ["FAKE_EXIT_AFTER_S"]))
        os._exit(int(os.environ["FAKE_EXIT_CODE"]))
    threading.Event().wait()


if __name__ == "__main__":
    main()
