# ingest/parser.py
#
# Parse tagger JSON output. Tolerates code fences and stray text.

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_tagger_output(raw: str) -> dict:
    """
    Extract the first JSON object in `raw`. Strips markdown code fences.
    Raises ValueError if nothing parses.
    """
    text = raw.strip()
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        return json.loads(text)
    except ValueError:
        pass

    obj_match = _JSON_OBJ_RE.search(text)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except ValueError as e:
            raise ValueError(f"could not parse tagger JSON: {e}") from e

    raise ValueError("tagger output had no JSON object")


def merge_tags(question: dict, tags: dict) -> dict:
    """Shallow-merge tagger-produced fields onto the question dict under _tags."""
    merged = dict(question)
    merged["_tags"] = {**merged.get("_tags", {}), **tags}
    return merged
