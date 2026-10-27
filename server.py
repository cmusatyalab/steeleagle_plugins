import contextlib
import signal
from pathlib import Path

import uriel
from common import BIND_ADDR, REMOTE_SERVER, VERIFIER_ENGINE

H264_DECODE_ENGINE = "h264-decode"

with uriel.Server(REMOTE_SERVER, bind_addr=BIND_ADDR, identity_dir="identity") as server, \
     contextlib.suppress(KeyboardInterrupt):
    server.add_engine(VERIFIER_ENGINE)
    server.add_engine(H264_DECODE_ENGINE)
    Path("certificate.der").write_bytes(server.certificate)
    print(f"server '{REMOTE_SERVER}' up on {BIND_ADDR}, engines: {VERIFIER_ENGINE}, {H264_DECODE_ENGINE}", flush=True)
    print(f"wrote certificate.der ({len(server.certificate)} bytes)", flush=True)
    signal.pause()
