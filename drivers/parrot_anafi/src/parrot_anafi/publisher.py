"""Publishes a stream on a Uriel producer from its own thread."""
import logging
import threading
import time

import uriel

logger = logging.getLogger('parrot-anafi/publisher')


class Publisher:
    """Calls `produce` at `fps` and publishes the bytes it returns, if any, as
    the Uriel producer `uri`, with the given payload type.

    The producer is created on the first input, sized to it plus `headroom`,
    and recreated if inputs grow past that, since a Uriel producer must be
    created, used and closed on one thread.
    """

    def __init__(self, uri, payload_type, fps, produce, headroom=0):
        self.uri = uri
        self.payload_type = payload_type
        self.period = 1.0 / fps
        self.produce = produce
        self.headroom = headroom
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name=f'publisher [{uri}]', daemon=True)
        self.thread.start()

    def stop(self):
        """Stops publishing and closes the producer."""
        self.stopped.set()
        self.thread.join()

    def run(self):
        producer = None
        max_input_size = 0
        next_time = time.monotonic()
        try:
            while not self.stopped.is_set():
                try:
                    data = self.produce()
                    if data is not None:
                        if len(data) > max_input_size:
                            if producer is not None:
                                producer.close()
                            max_input_size = len(data) + self.headroom
                            producer = uriel.Producer(
                                self.uri,
                                input_type=self.payload_type,
                                max_input_size=max_input_size,
                            )
                            logger.info('publishing %s on %s, up to %d bytes each',
                                        self.payload_type, self.uri, max_input_size)
                        producer.send(data)
                except uriel.AgainError:
                    pass  # no free input buffer; newer inputs follow
                except Exception as e:
                    logger.warning(f'could not publish on {self.uri}, reason: {e}')
                next_time += self.period
                sleep_time = next_time - time.monotonic()
                if sleep_time > 0:
                    self.stopped.wait(sleep_time)
                else:
                    # Producing overran the period
                    next_time = time.monotonic()
        finally:
            if producer is not None:
                producer.close()
