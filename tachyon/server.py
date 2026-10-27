import contextlib
import signal

import uriel
from common import EMBED_ENGINE, SCORE_ENGINE, SERVER

H264_ENCODE_ENGINE = "h264-encode"

with uriel.Server(SERVER) as server, contextlib.suppress(KeyboardInterrupt):
    server.add_engine(EMBED_ENGINE)
    server.add_engine(SCORE_ENGINE)
    server.add_engine(H264_ENCODE_ENGINE)
    print(f"server '{SERVER}' up, engines: {EMBED_ENGINE}, {SCORE_ENGINE}, {H264_ENCODE_ENGINE}", flush=True)
    signal.pause()
