"""The verifier engine: asks a vision-language model served by Ollama (Qwen3-VL
8B Instruct by default) whether each RawFrame matches the prompt, forwards the frame
if it does and drops it if not.

The prompt is text, or a reference image; with an image, the model is shown
the reference first and the frame second. Either way it answers yes or no,
and every input gets a GenericResult holding the JSON object
{"match": true|false}, so cliped's send queue sees a RESULT event for each
tile it sent, and cliped learns from it.

Nothing here imports torch: the verifier runs on a server without the edge
filter's models.
"""
import base64
import json
import logging
import time
import urllib.error
import urllib.request

import cv2
import uriel

from cliped.engine import GENERIC_RESULT_TYPE, POLL_INTERVAL_S, RAW_FRAME_TYPE, FrameError, read_frame
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("cliped.verifier")

DEFAULT_MODEL = "qwen3-vl:8b-instruct"
DEFAULT_HOST = "http://localhost:11434"
# Room for the result; the verifier forwards its input as is.
MAX_OUTPUT_SIZE = 64 << 10
# How long pulling the model may take, the first time it runs on a server
PULL_TIMEOUT_S = 3600
# Longest side of the reference image sent to the model, in pixels. A phone
# photo at full size costs a 30 MB PNG and twice the prompt tokens on every
# frame, for no better answer.
REFERENCE_MAX_SIDE = 448

TEXT_QUESTION = (
    "This is a tile cut from an aerial drone photo. Does it show {prompt}? "
    "Answer with only yes or no."
)
# The reference is usually a ground-level photo, and the tile looks down on
# the target from far above: say so, and ask for the same target, not its
# category, so look-alikes (e.g. real people for a mannequin) are rejected.
IMAGE_QUESTION = (
    "Image 1: a reference photo of a target, taken from the ground. "
    "Image 2: a tile from an aerial drone photo, seen from above. "
    "Does image 2 contain this same target (same appearance, not merely the same category), "
    "possibly small, rotated, lying down, or partly out of frame? Answer with only yes or no."
)


class OllamaError(Exception):
    pass


class Ollama:
    """A client of the Ollama server at `host`, for `model`."""

    def __init__(self, host=DEFAULT_HOST, model=DEFAULT_MODEL, timeout_s=30.0):
        self.host = host.rstrip("/")
        if "://" not in self.host:
            self.host = f"http://{self.host}"
        self.model = model
        self.timeout_s = timeout_s

    def _post(self, path, body, timeout_s):
        request = urllib.request.Request(
            self.host + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace").strip()
            raise OllamaError(f"{path}: HTTP {err.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise OllamaError(f"{path}: {err}") from None

    def ensure_model(self):
        """Pulls the model if the server does not have it."""
        try:
            self._post("/api/show", {"model": self.model}, self.timeout_s)
            return
        except OllamaError as err:
            if "HTTP 404" not in str(err):
                raise
        logger.info("pulling %s from the Ollama registry; this can take a while", self.model)
        status = self._post("/api/pull", {"model": self.model, "stream": False}, PULL_TIMEOUT_S)
        if status.get("status") != "success":
            raise OllamaError(f"pulling {self.model}: {status}")

    def ask(self, question, images):
        """The model's answer to `question` about `images` (encoded image
        files, e.g. PNG or JPEG bytes)."""
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": question,
                "images": [base64.b64encode(image).decode() for image in images],
            }],
            "stream": False,
            "keep_alive": -1,  # keep it loaded between frames
            "options": {"temperature": 0, "num_predict": 8},
        }
        reply = self._post("/api/chat", body, self.timeout_s)
        try:
            return reply["message"]["content"]
        except (KeyError, TypeError):
            raise OllamaError(f"/api/chat: unexpected reply {reply!r}") from None


def parse_answer(answer):
    """True for a yes, False for a no, None for anything else."""
    word = answer.strip().lstrip("*\"'`").lower()
    if word.startswith("yes"):
        return True
    if word.startswith("no"):
        return False
    return None


def shrink(rgb, max_side=REFERENCE_MAX_SIDE):
    """`rgb` scaled down, keeping its aspect ratio, so its longer side is at
    most `max_side` pixels; as is if it already fits."""
    height, width = rgb.shape[:2]
    scale = max_side / max(height, width)
    if scale >= 1:
        return rgb
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)


def encode_png(rgb):
    ok, data = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise FrameError("failed to encode the frame for the model")
    return data.tobytes()


class Verifier:
    """Decides whether inputs match the prompt. `ask(question, images)` is
    the model (Ollama.ask). With `reference`, an encoded image file, the
    prompt is that image, named `label`; otherwise it is the text `label`.
    `tile_log` (files.TileLog), if given, gets each input and each frame
    forwarded."""

    def __init__(self, ask, *, label, reference=None, tile_log=None):
        self.ask = ask
        self.label = label
        self.reference = reference
        self.tile_log = tile_log
        self.frames = 0
        self.matched = 0

    def question(self, png):
        if self.reference is not None:
            return IMAGE_QUESTION, [self.reference, png]
        return TEXT_QUESTION.format(prompt=self.label), [png]

    def on_input(self, input):
        """Whether to forward `input`, and the serialized GenericResult.
        Raises FrameError if `input` is not an RGB RawFrame, and OllamaError
        if the model could not be asked."""
        frame, rgb = read_frame(bytes(input.data))
        if self.tile_log is not None:
            self.tile_log.input(frame.id, rgb)
        started = time.perf_counter()
        answer = self.ask(*self.question(encode_png(rgb)))
        match = parse_answer(answer)
        if match is None:
            logger.warning("frame %d: the model answered %r, not yes or no; dropping it", frame.id, answer)
        match = bool(match)
        self.frames += 1
        self.matched += match
        if match and self.tile_log is not None:
            self.tile_log.output(frame.id, 0, rgb)

        logger.info("frame %d (%dx%d): %s in %.0f ms; %d of %d matched", frame.id, rgb.shape[1], rgb.shape[0],
                    "match, forwarding it" if match else "no match, dropping it",
                    (time.perf_counter() - started) * 1e3, self.matched, self.frames)
        return match, result_pb2.GenericResult(payload=json.dumps({"match": match}).encode()).SerializeToString()


def run(name, verifier, *, stop, on_ready=None):
    """Serves the engine `name` until the threading.Event `stop` is set."""
    with uriel.Engine(name, result_type=GENERIC_RESULT_TYPE, max_output_size=MAX_OUTPUT_SIZE) as engine:
        logger.info("engine %s ready; verifying %s %r", name,
                    "image" if verifier.reference is not None else "prompt", verifier.label)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            input = engine.next(POLL_INTERVAL_S)
            if input is None:
                continue
            if input.payload_type.replace(".", "/") != RAW_FRAME_TYPE:
                logger.warning("input %d is a %r, not a RawFrame; dropping it", input.id.seq, input.payload_type)
                engine.drop()
                continue
            try:
                match, result = verifier.on_input(input)
            except FrameError as err:
                logger.warning("input %d: %s; dropping it", input.id.seq, err)
                engine.drop()
                continue
            except Exception:
                logger.exception("input %d: failed to verify it; dropping it", input.id.seq)
                engine.drop()
                continue
            if match:
                engine.forward(result=result)
            else:
                engine.drop(result=result)
