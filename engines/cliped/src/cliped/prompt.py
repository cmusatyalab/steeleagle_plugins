"""The verifier's prompt heartbeat, as cliped and box_rerank read it.

Every heartbeat, the verifier publishes a DERIVED event of type PROMPT_TYPE
holding, as UTF-8 JSON, its prompt's class and description (see
verifier.py). An engine that follows the verifier's prompt subscribes to the
verifier and takes each such event with heartbeat_prompt.
"""
import json
import logging

import uriel

logger = logging.getLogger("cliped")

# The type of the heartbeat's DERIVED events: UTF-8 JSON {"class": ...,
# "description": ...} (verifier.heartbeat_payload)
PROMPT_TYPE = "cliped/Prompt"


def type_name(payload_type):
    """Engines write a type's name with slashes or, as detection-qnn does,
    dots."""
    return payload_type.replace(".", "/")


def read_prompt(text):
    """The text prompt and class prompt of the verifier's heartbeat `text`:
    JSON {"class", "description"}, the description CLIP ranks by (else the
    class) and the class for the gate (None if it has none, or the same
    text); or, from a verifier before the JSON, the text prompt alone."""
    try:
        payload = json.loads(text)
    except ValueError:
        return text, None
    if not isinstance(payload, dict):
        return text, None
    clean = lambda value: " ".join(value.split()) if isinstance(value, str) else ""
    description, class_prompt = clean(payload.get("description")), clean(payload.get("class"))
    prompt = description or class_prompt
    return prompt, (class_prompt if class_prompt and class_prompt.lower() != prompt.lower() else None)


def is_heartbeat(event, verifier):
    """Whether `event` is a prompt heartbeat of the engine `verifier`."""
    return (event.kind == uriel.EventKind.DERIVED and event.engine == verifier
            and type_name(event.payload_type or "") == PROMPT_TYPE)


def heartbeat_prompt(event):
    """The (text prompt, class prompt) of the heartbeat `event` (read_prompt),
    or None if its payload isn't UTF-8."""
    try:
        text = bytes(event.result or b"").decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("the verifier's prompt heartbeat isn't UTF-8; ignoring it")
        return None
    return read_prompt(text)
