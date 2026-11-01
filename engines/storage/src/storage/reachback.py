"""Reachback: store every frame and drop it, and send the remote engine the
stored frame it asks for.

The remote engine asks with a DERIVED event whose result is a
ReachbackRequest. The engine picks the frame it asks for when the request
arrives, and sends it in place of the next input. Requests are answered in
the order they arrive, one per input.
"""

import collections
import logging

import uriel
from google.protobuf.message import DecodeError

from storage import storage_pb2
from storage.priority_reintegration import result_type_name
from storage.store import INDEX_FIELDS, FrameError, FrameKey, frame_index, read_frame

logger = logging.getLogger("storage.reachback")

REACHBACK_REQUEST_TYPE = "storage/v1/ReachbackRequest"
REACHBACK_REQUEST = "ReachbackRequest"


def request_query(request):
    """The index fields a ReachbackRequest sets, by name."""
    return {name: getattr(request, name) for name in INDEX_FIELDS if request.HasField(name)}


class Reachback:
    def __init__(self, store, *, remote, max_frame_size):
        self.store = store
        self.remote = remote
        self.max_frame_size = max_frame_size
        # Keys of the frames picked for requests, waiting for an input to go
        # out in place of.
        self.pending = collections.deque()

    def on_event(self, event):
        if event.engine != self.remote or event.kind != uriel.EventKind.DERIVED:
            return
        # A derived result without a type is read as a request too
        if event.payload_type and result_type_name(event.payload_type) != REACHBACK_REQUEST:
            return
        try:
            request = storage_pb2.ReachbackRequest.FromString(event.result)
        except DecodeError as err:
            logger.warning("bad ReachbackRequest from %s: %s", self.remote, err)
            return
        stored = self.pick(request)
        if stored is not None:
            self.pending.append(stored.key)

    def pick(self, request):
        """The stored frame that best matches `request`, or None."""
        if request.HasField("sequence_number"):
            stored = self.store.by_seq(request.sequence_number)
            if stored is None:
                logger.warning("reachback: no frame %d is stored", request.sequence_number)
            return stored
        query = request_query(request)
        if not query:
            logger.warning("reachback: the request sets no field")
            return None
        stored = self.store.best_match(query)
        if stored is None:
            logger.warning("reachback: no stored frame has all of %s", sorted(query))
        else:
            logger.info("reachback: frame %d best matches %s", stored.key.seq, query)
        return stored

    def on_input(self, input):
        try:
            frame = read_frame(input.payload_type, input.data)
        except FrameError as err:
            logger.warning("input %d: %s", input.id.seq, err)
        else:
            if len(input.data) > self.max_frame_size:
                logger.warning(
                    "input %d is %d bytes, over max_output_size; not storing it",
                    input.id.seq,
                    len(input.data),
                )
            else:
                self.store.add(
                    FrameKey.of(input.id), input.payload_type, input.data, frame_index(frame)
                )
        while self.pending:
            stored = self.store.get(self.pending.popleft())
            if stored is not None:
                logger.info("reachback: sending frame %d as input %d", stored.key.seq, input.id.seq)
                return stored
        return None
