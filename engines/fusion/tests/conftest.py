import uriel

PRODUCER = "camera"
SESSION = 0xDEADBEEF_CAFEF00D  # above 2**63, as uint64 sessions can be
LOCAL = "100.64.0.1.detection"
REMOTE = "100.64.0.2.detection"
DETECTION_RESULT_TYPE = "steeleagle_protocol/v1/messages/result/DetectionResult"


def input_id(seq):
    return uriel.InputId(PRODUCER, SESSION, seq)


def make_event(
    kind,
    engine,
    seq=0,
    result=b"",
    outcome=uriel.Outcome.FORWARD,
    payload_type=DETECTION_RESULT_TYPE,
):
    return uriel.Event(
        kind=kind,
        lost=0,
        id=input_id(seq),
        engine=engine,
        flow="flow",
        outcome=outcome,
        payload_type=payload_type if result else "",
        result=result,
        at_ns=0,
        offered_at_ns=None,
        received_at_ns=None,
    )


class Clock:
    def __init__(self):
        self.ns = 1_000_000_000

    def __call__(self):
        return self.ns

    def advance_ms(self, ms):
        self.ns += ms * 1_000_000
