"""The verifier engine: asks a vision-language model served by Ollama (Qwen3-VL
30B-A3B Instruct by default) whether each RawFrame matches the prompt, forwards the frame
if it does and drops it if not.

The prompt is text, or a reference image; with an image, the model is shown
the reference first and the frame second. Either way it answers yes or no,
and every input gets a GenericResult holding the JSON object
{"match": true|false}, so cliped's send queue sees a RESULT event for each
tile it sent, and cliped learns from it.

With a live directory (LiveDir), the question and the prompt can change while
it runs: question.txt, and prompt.txt or prompt.jpg/.jpeg, are read when it
starts and again whenever they change. Every heartbeat (PromptKeeper), it
publishes a DERIVED event of type PROMPT_TYPE holding the prompt's
description as UTF-8 text: the text prompt, or, for an image, the model's
description of it. box_rerank follows it, so it ranks its boxes by the
verifier's prompt and starts learning over when it changes.

Nothing here imports torch: the verifier runs on a server without the edge
filter's models.
"""
import base64
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import uriel

from cliped.engine import GENERIC_RESULT_TYPE, POLL_INTERVAL_S, RAW_FRAME_TYPE, FrameError, read_frame
from steeleagle_protocol.v1.messages.result import result_pb2

logger = logging.getLogger("cliped.verifier")

# A mixture of experts: 30B parameters, ~3B active per token, so it answers as
# fast as the 8B (317 ms against 358 ms per crop on an RTX 4090) and fits in
# 19 GB at NUM_CTX. On 147 labeled live crops of a robot dog it said yes to
# 46 of 67 dog crops (the 8B: 18) and to 4 of 80 others (the 8B: 2), and kept
# finding the dog in poor light, where the 8B found none
DEFAULT_MODEL = "qwen3-vl:30b-a3b-instruct"
DEFAULT_HOST = "http://localhost:11434"
# Room for the result; the verifier forwards its input as is.
MAX_OUTPUT_SIZE = 64 << 10
# How long pulling the model may take, the first time it runs on a server
PULL_TIMEOUT_S = 3600
# The type of the heartbeat's DERIVED events: the prompt's description, as
# UTF-8 text
PROMPT_TYPE = "cliped/Prompt"
# Seconds between heartbeats
HEARTBEAT_S = 1.0
# The live directory's files: the question, and the prompt, an image (which
# wins over the text when both are there) or text
QUESTION_FILE = "question.txt"
PROMPT_TEXT_FILE = "prompt.txt"
PROMPT_IMAGE_FILES = ("prompt.jpg", "prompt.jpeg")
# Asked about an image prompt once, for the heartbeat: a phrase CLIP can rank
# box_rerank's crops by, which it does far better with text than with an image
DESCRIBE_QUESTION = (
    "Describe the person or object in this image as a short phrase for an image search that must find it "
    "from a drone, e.g. 'a person in a red shirt and green shorts' or 'a quadruped robot dog'. Describe only "
    "the person or object itself, not the ground, background or what it is doing; only colors and shapes "
    "visible from far above. Answer with the phrase only, under 15 words."
)
# Tokens of context the model is loaded with. A request is one or two small
# images, a question and an answer of a few words, well under 4096; Ollama's
# default (the model's full context, 262144 for Qwen3-VL) reserves a KV cache
# that took qwen3-vl:8b-instruct to 42 GB of GPU memory, against 5.8 GB at
# 4096, and leaves no room for a larger model on lambda's two 24 GB GPUs
NUM_CTX = 4096
# Longest side of the reference image sent to the model, in pixels. A phone
# photo at full size costs a 30 MB PNG and twice the prompt tokens on every
# frame, for no better answer.
REFERENCE_MAX_SIDE = 448

TEXT_QUESTION = (
    "This is a tile cut from an aerial drone photo. Does it show {prompt}? "
    "Answer with only yes or no."
)
# The reference is usually a ground-level photo, and the tile is a small,
# low-resolution view from above: say so, and that the target never looks
# exactly like the reference, so the model doesn't demand the same appearance.
# Asking for "this target" still rejects most look-alikes. On the mannequin
# tiles cliped sent (parkinglot, traintrack, street) it kept 103/105 real ones
# and said yes to 2/19 tiles of people and 28/303 of nothing; the earlier
# "same appearance, not merely the same category" kept only 86/105.
IMAGE_QUESTION = (
    "Image 1 is a reference photo of a target, taken from the ground. Image 2 is a low-resolution tile cut "
    "from an aerial drone photo, seen from above. The target in image 2 would be small and will never look "
    "exactly like the reference. Does image 2 contain this target, possibly rotated, lying down or partly "
    "out of frame? Answer with only yes or no."
)
# For box_rerank's crops: a detection with context around it, its box drawn
# in red. The context helps the model recognize a small object, and the box
# says which object is asked about when another (e.g. the target next to a
# distractor) is in the crop too.
BOX_QUESTION = (
    "This image is cut from an aerial drone video. Is the object inside the red box {prompt}? "
    "Answer with only yes or no."
)
# box_rerank's crops with a reference image (an individual, e.g. one person
# among several): as IMAGE_QUESTION, about the boxed object only, so a crop
# whose context holds the target next to another object is a no for the
# other one
BOX_IMAGE_QUESTION = (
    "Image 1 is a reference photo of a target, taken from the ground. Image 2 is cut from an aerial drone "
    "video, seen from above, with one object marked by a red box. That object would be small and will never "
    "look exactly like the reference. Is the object inside the red box this target, possibly rotated, lying "
    "down or partly out of frame? Judge only the object inside the red box. Answer with only yes or no."
)


class OllamaError(Exception):
    pass


class Ollama:
    """A client of the Ollama server at `host`, for `model`, loaded with a
    context of `num_ctx` tokens (0 for the server's default)."""

    def __init__(self, host=DEFAULT_HOST, model=DEFAULT_MODEL, timeout_s=30.0, num_ctx=NUM_CTX):
        self.host = host.rstrip("/")
        if "://" not in self.host:
            self.host = f"http://{self.host}"
        self.model = model
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx

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

    def ask(self, question, images, max_tokens=8):
        """The model's answer to `question` about `images` (encoded image
        files, e.g. PNG or JPEG bytes), of at most `max_tokens` tokens."""
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": question,
                "images": [base64.b64encode(image).decode() for image in images],
            }],
            "stream": False,
            "keep_alive": -1,  # keep it loaded between frames
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        if self.num_ctx:
            # Every request asks for the same context: Ollama reloads the
            # model when it changes
            body["options"]["num_ctx"] = self.num_ctx
        reply = self._post("/api/chat", body, self.timeout_s)
        try:
            return reply["message"]["content"]
        except (KeyError, TypeError):
            raise OllamaError(f"/api/chat: unexpected reply {reply!r}") from None

    def describe(self, image):
        """A short description of what the encoded image file `image` shows,
        for a text prompt."""
        return clean_description(self.ask(DESCRIBE_QUESTION, [image], max_tokens=40))


def clean_description(text):
    """The model's description as one line, without quotes or a full stop."""
    return " ".join(text.split()).strip("\"'`*. ")


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


def enlarge(rgb, min_side):
    """`rgb` scaled up, keeping its aspect ratio, so its shorter side is at
    least `min_side` pixels; as is if it already is, or if `min_side` is 0. A
    crop is sent small to save the link, and enlarged here, where it costs
    nothing, so the model sees the object over more patches."""
    height, width = rgb.shape[:2]
    if not min_side or min(height, width) >= min_side:
        return rgb
    scale = min_side / min(height, width)
    return cv2.resize(rgb, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_CUBIC)


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
    forwarded. `question`, if given, replaces TEXT_QUESTION or
    IMAGE_QUESTION, with {prompt} in it replaced by `label`: wording for one
    target (e.g. "the same kind of animal" for an animal) can tell it from
    look-alikes better than the generic questions; "box" is BOX_QUESTION or,
    with a reference, BOX_IMAGE_QUESTION. Each frame is enlarged to at least
    `min_side` pixels on its shorter side before the model sees it (0 for as
    is).

    `description` is the prompt as text, for the heartbeat: the label of a
    text prompt, or, for an image, its description once PromptKeeper has
    asked for it (None until then)."""

    def __init__(self, ask, *, label, reference=None, tile_log=None, question=None, min_side=0):
        self.ask = ask
        self.tile_log = tile_log
        self.min_side = min_side
        self.frames = 0
        self.matched = 0
        self.set_prompt(label, reference)
        self.set_question(question)

    @property
    def has_prompt(self):
        return self.reference is not None or bool(self.label)

    def set_prompt(self, label, reference=None):
        """Verifies against the image `reference` (encoded), named `label`,
        from now on, or the text `label` without one."""
        self.label = label
        self.reference = reference
        self.description = None if reference is not None else (label or None)

    def set_question(self, question):
        """Asks `question` from now on, or the default one for None or "". """
        self.custom_question = question or None

    def question(self, png):
        custom = self.custom_question
        if custom == "box":
            custom = BOX_IMAGE_QUESTION if self.reference is not None else BOX_QUESTION
        if self.reference is not None:
            return (custom or IMAGE_QUESTION).format(prompt=self.label), [self.reference, png]
        return (custom or TEXT_QUESTION).format(prompt=self.label), [png]

    def on_input(self, input):
        """Whether to forward `input`, and the serialized GenericResult.
        Raises FrameError if `input` is not an RGB RawFrame, and OllamaError
        if the model could not be asked."""
        frame, rgb = read_frame(bytes(input.data))
        if self.tile_log is not None:
            self.tile_log.input(frame.id, rgb)
        started = time.perf_counter()
        answer = self.ask(*self.question(encode_png(enlarge(rgb, self.min_side))))
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


def valid_question(question):
    """Whether `question` is a usable question: only {prompt} in braces."""
    try:
        question.format(prompt="")
    except (KeyError, IndexError, ValueError):
        return False
    return True


class LiveDir:
    """The live directory `path`: the question and prompt files, as they
    change. A file that is removed changes nothing: the verifier keeps what it
    had."""

    def __init__(self, path):
        self.path = Path(path)
        self._question = None  # the (mtime_ns, size) of question.txt last read
        self._prompt = None  # (file name, (mtime_ns, size)) of the prompt last read

    def _signature(self, name):
        try:
            stat = (self.path / name).stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def poll(self):
        """What changed since the last poll: (question, prompt). question is
        question.txt's text if it is new or changed, else None. prompt is the
        prompt file's if it is new or changed, else None: ("image", path), the
        newest of prompt.jpg and prompt.jpeg if either is there, or ("text",
        text) of prompt.txt. A prompt the caller couldn't use can be read
        again with forget_prompt."""
        question = None
        signature = self._signature(QUESTION_FILE)
        if signature is not None and signature != self._question:
            try:
                question = (self.path / QUESTION_FILE).read_text(encoding="utf-8").strip()
                self._question = signature
            except (OSError, UnicodeDecodeError) as err:
                logger.warning("live: can't read %s: %s", QUESTION_FILE, err)
        images = [(signature, name) for name in PROMPT_IMAGE_FILES
                  if (signature := self._signature(name)) is not None]
        if images:
            signature, name = max(images)
        elif (signature := self._signature(PROMPT_TEXT_FILE)) is not None:
            name = PROMPT_TEXT_FILE
        else:
            return question, None
        if (name, signature) == self._prompt:
            return question, None
        self._prompt = (name, signature)
        if name == PROMPT_TEXT_FILE:
            try:
                text = (self.path / name).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as err:
                logger.warning("live: can't read %s: %s", name, err)
                self._prompt = None
                return question, None
            return question, (("text", text) if text else None)
        return question, ("image", self.path / name)

    def forget_prompt(self):
        self._prompt = None


class PromptKeeper:
    """Keeps the verifier's prompt and question up to date with the live
    directory `live` (a LiveDir, or None), and gives the heartbeat's payload
    every `interval_s` seconds: the prompt's description, as UTF-8. An image
    prompt is read with `load_image(path)`, which returns it encoded (and
    raises on a file it can't read), and described with `describe(image)`
    (Ollama.describe), once; if that fails, it is asked again at the next
    heartbeat."""

    def __init__(self, verifier, *, live=None, describe=None, load_image=None, interval_s=HEARTBEAT_S):
        self.verifier = verifier
        self.live = live
        self.describe = describe
        self.load_image = load_image
        self.interval_s = interval_s
        self._next = None

    def update(self):
        """Applies what changed in the live directory, if any; returns
        whether anything did."""
        if self.live is None:
            return False
        question, prompt = self.live.poll()
        changed = False
        if question is not None:
            if valid_question(question):
                self.verifier.set_question(question)
                logger.info("live: question now %r", question or "(the default)")
                changed = True
            else:
                logger.warning("live: ignoring question %r: only {prompt} may be in braces", question)
        if prompt is not None:
            kind, value = prompt
            if kind == "text":
                self.verifier.set_prompt(value)
                logger.info("live: prompt now the text %r", value)
                changed = True
            else:
                try:
                    reference = self.load_image(value)
                except Exception as err:
                    logger.warning("live: can't read %s (%s); trying again", value.name, err)
                    self.live.forget_prompt()
                else:
                    self.verifier.set_prompt(value.name, reference)
                    logger.info("live: prompt now the image %s", value.name)
                    changed = True
        return changed

    def _describe(self):
        v = self.verifier
        if v.description is not None or v.reference is None or self.describe is None:
            return
        try:
            v.description = clean_description(self.describe(v.reference)) or None
        except Exception as err:
            logger.warning("couldn't describe the image %s (%s); asking again at the next heartbeat", v.label, err)
            return
        logger.info("the image %s described as %r", v.label, v.description)

    def step(self, now):
        """The heartbeat's payload if one is due at `now` (seconds,
        time.monotonic()), else None: before it, the live directory is read
        and an image described. None too while there is no description."""
        if self._next is not None and now < self._next:
            return None
        self._next = now + self.interval_s
        self.update()
        self._describe()
        description = self.verifier.description
        return description.encode("utf-8") if description else None


def run(name, verifier, *, stop, keeper=None, on_ready=None):
    """Serves the engine `name` until the threading.Event `stop` is set,
    publishing `keeper`'s heartbeat (a PromptKeeper; one without a live
    directory by default). A DERIVED event is about an input in a flow, so the
    heartbeat starts once the first input has come, and is about the latest;
    the engine subscribes to its own events to learn the flow."""
    keeper = keeper or PromptKeeper(verifier)
    with uriel.Engine(name, result_type=GENERIC_RESULT_TYPE, max_output_size=MAX_OUTPUT_SIZE) as engine:
        engine.subscribe(name)
        flow, last_id = None, None
        logger.info("engine %s ready; verifying %s %r; a heartbeat every %gs", name,
                    "image" if verifier.reference is not None else "prompt", verifier.label, keeper.interval_s)
        if on_ready is not None:
            on_ready()
        while not stop.is_set():
            engine.wait(POLL_INTERVAL_S)
            while (event := engine.poll_event()) is not None:
                flow = event.flow or flow
            payload = keeper.step(time.monotonic())
            if payload is not None and flow is not None and last_id is not None:
                try:
                    engine.publish_derived(last_id, flow, payload, result_type=PROMPT_TYPE)
                except Exception:
                    logger.exception("failed to publish the prompt heartbeat")
            input = engine.poll()
            if input is None:
                continue
            last_id = input.id
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
