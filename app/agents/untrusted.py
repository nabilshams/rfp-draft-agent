"""Marking untrusted text inside a prompt.

Requirement text, uploaded documents, retrieved knowledge-base chunks and user-supplied context are all
data that somebody else wrote, and any of it may contain instructions aimed at the model ("ignore the
above and ..."). Each piece is wrapped in a fence whose label includes a random nonce, and the agent's
instructions say that fenced text is material to work on, never instructions to follow.

The nonce is generated per fence, so text inside it cannot know the closing marker; any look-alike
marker already present in the text is defused as well.
"""

from __future__ import annotations

import re
import secrets

# The marker characters, so that the text itself can never contain a fence boundary.
_MARKER = re.compile(r"<<<|>>>")


def fence(label: str, text: str) -> str:
    """Wrap *text* so the model can tell it apart from instructions."""
    nonce = secrets.token_hex(6)
    cleaned = _MARKER.sub(lambda m: "\u2039\u2039\u2039" if m.group() == "<<<" else "\u203a\u203a\u203a", text or "")
    return f"<<<{label}:{nonce}\n{cleaned}\n{label}:{nonce}>>>"
