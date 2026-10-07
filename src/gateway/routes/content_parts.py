"""OpenAI content parts: what's accepted, and how it's sanitized and stored (D-011).

A message's content is a string or a list of parts. Supported parts:
- {"type": "text", "text": ...}: sanitized and PII-scrubbed like plain text
- {"type": "image_url", "image_url": {"url": "data:image/...;base64,..."}}:
  an inline image, passed to the engine unchanged

Rejected with a validation error, never silently dropped:
- image URLs (http/https): the gateway doesn't fetch on a client's behalf
  (internal addresses, egress policy, a URL's content can change)
- any other part type (input_audio, file, ...): not supported on chat yet
"""

import re
from typing import Any

from gateway.errors import ValidationError
from gateway.security import Sanitizer

_DATA_IMAGE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,", re.IGNORECASE)


def check_content_parts(content: Any) -> None:
    """Refuse parts the gateway would otherwise lose.

    Raises:
        ValidationError: An image URL or an unsupported part type.
    """
    if not isinstance(content, list):
        return
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            continue
        if kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if _DATA_IMAGE.match(url):
                continue
            raise ValidationError(
                message="Image URLs aren't fetched by the gateway; send the image inline "
                "as a data URL (data:image/png;base64,...)"
            )
        raise ValidationError(
            message=f"Content part type {kind!r} isn't supported on this route "
            "(supported: text, inline image_url)"
        )


def sanitize_content(sanitizer: Sanitizer, content: Any) -> Any:
    """Sanitize text, keeping the content's shape (string, parts, or None)."""
    if isinstance(content, str):
        return sanitizer.sanitize(content).sanitized
    if isinstance(content, list):
        return [
            {**part, "text": sanitizer.sanitize(part.get("text") or "").sanitized}
            if part.get("type") == "text"
            else part
            for part in content
        ]
    return content if content is not None else ""


def text_only(messages: list[dict]) -> list[dict]:
    """Messages with parts flattened to their text (for text-only analyzers)."""
    flattened = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text") or "" for p in content if p.get("type") == "text")
        flattened.append({**message, "content": content})
    return flattened


def redact_media(value: Any) -> Any:
    """Replace inline media with a short description, for anything stored (D-023)."""
    if isinstance(value, str):
        match = _DATA_IMAGE.match(value)
        if match:
            size_kb = len(value) * 3 / 4 / 1024
            return f"[{match.group(1)}, {size_kb:.1f} KB]"
        return value
    if isinstance(value, list):
        return [redact_media(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_media(v) for k, v in value.items()}
    return value
