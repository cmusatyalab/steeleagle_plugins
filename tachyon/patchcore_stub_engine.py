"""Dummy stand-in for the real PatchCore scoring engine. Parses
FrameWithEmbedding, prints what it got, and always drops -- just proves the
C++ embed engine -> this engine wiring and the protobuf parsing work, without
needing faiss/anomalib/torch installed yet.
"""
import uriel
from steeleagle_protocol.v1.messages.result import result_pb2
from common import MAX_EMBED_SIZE, SCORE_ENGINE

with uriel.Engine(SCORE_ENGINE, max_output_size=MAX_EMBED_SIZE) as engine:
    print("patchcore-score STUB engine ready", flush=True)
    for input in engine:
        combined = result_pb2.FrameWithEmbedding()
        combined.ParseFromString(bytes(input.data))

        n_floats = len(combined.embedding.data)
        shape = list(combined.embedding.shape)
        frame_bytes = len(combined.encoded_frame)

        print(
            f"got FrameWithEmbedding: frame={frame_bytes} bytes, "
            f"embedding shape={shape}, {n_floats} floats",
            flush=True,
        )
        engine.drop(result=b'{"stub": true}')
