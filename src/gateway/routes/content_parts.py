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
        if not isinstance(part, dict):
            raise ValidationError(
                message="Content parts must be objects like "
                '{"type": "text", "text": ...}; got a bare value'
            )
        kind = part.get("type")
        if kind == "text":
            if not isinstance(part.get("text", ""), str):
                raise ValidationError(message='A text part\'s "text" must be a string')
            continue
        if kind == "image_url":
            image_url = part.get("image_url")
            # Accept both OpenAI shapes: {"url": ...} and the bare string
            # some clients send. Anything else is a 4xx, never a 500.
            if isinstance(image_url, str):
                url = image_url
            elif isinstance(image_url, dict):
                url = image_url.get("url", "")
            else:
                raise ValidationError(
                    message='An image_url part needs "image_url" as an object '
                    '{"url": "data:image/..."} or a data-URL string'
                )
            if isinstance(url, str) and _DATA_IMAGE.match(url):
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
        normalized = []
        for part in content:
            if not isinstance(part, dict):
                normalized.append(part)  # check_content_parts rejects these
            elif part.get("type") == "text":
                normalized.append(
                    {**part, "text": sanitizer.sanitize(part.get("text") or "").sanitized}
                )
            elif part.get("type") == "image_url" and isinstance(part.get("image_url"), str):
                # Normalize the bare-string shape so adapters see one shape
                normalized.append({**part, "image_url": {"url": part["image_url"]}})
            else:
                normalized.append(part)
        return normalized
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
