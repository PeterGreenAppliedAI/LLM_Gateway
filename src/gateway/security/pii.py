"""PII detection and scrubbing for request content.

Detects and optionally replaces personally identifiable information:
- Email addresses
- Phone numbers (US formats)
- Social Security Numbers
- Credit card numbers
- IP addresses

Detection always runs to flag PII in security alerts.
Scrubbing (replacement with placeholders) is per-route configurable.
"""

import json
import re
import time
from dataclasses import dataclass, field


@dataclass
class PIIMatch:
    """A single PII detection."""

    pii_type: str  # EMAIL, PHONE, SSN, CREDIT_CARD, IP_ADDRESS
    start: int  # Position in text
    end: int
    placeholder: str  # e.g., "[EMAIL]"


@dataclass
class PIIScanResult:
    """Result of PII scanning on a single text."""

    has_pii: bool
    detections: list[PIIMatch] = field(default_factory=list)
    scrubbed_text: str | None = None  # Only populated when scrubbing is requested
    scan_time_ms: float = 0.0

    @property
    def detection_count(self) -> int:
        return len(self.detections)

    def to_dict(self) -> dict:
        return {
            "has_pii": self.has_pii,
            "detection_count": self.detection_count,
            "pii_types": list(set(d.pii_type for d in self.detections)),
            "scan_time_ms": round(self.scan_time_ms, 3),
        }


@dataclass
class PIIFinding:
    """One scanned text and exactly where it came from.

    Audit rows are built from these, not by matching results to messages by
    position: that silently skipped content parts (zero PII events for an
    email in a multimodal message) and, for batches that kept only results
    with detections, paired them with the wrong inputs (wrong index, hash of
    the wrong text).
    """

    message_index: int  # position in the request's messages, prompts or inputs
    role: str | None
    text: str  # the original text; detection offsets refer to it
    result: PIIScanResult
    part_index: int | None = None  # content part within the message, if any

    @property
    def has_pii(self) -> bool:
        return self.result.has_pii

    @property
    def detection_count(self) -> int:
        return self.result.detection_count

    @property
    def scrubbed_text(self) -> str | None:
        return self.result.scrubbed_text


# Pre-compiled PII patterns
# Order matters — more specific patterns first to avoid partial matches
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    # SSN: 123-45-6789 or 123 45 6789 (but NOT 9 digits with no separators to reduce false positives)
    ("SSN", re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b")),
    # Credit card: 4 groups of 4 digits, with optional separators
    ("CREDIT_CARD", re.compile(r"\b(?:\d{4}[-\s]){3}\d{4}\b")),
    # Email. Lengths are bounded by the standard's own limits (64 before the
    # "@", 253 after): unbounded runs made scanning quadratic, so text like
    # "a.a.a.…" took ~20 s per 100k characters, which is why scans used to be
    # cut off at 100k (and why text past the cut reached engines unscrubbed)
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,63}\b")),
    # Phone: US formats - (123) 456-7890, 123-456-7890, +1 123 456 7890, etc.
    ("PHONE", re.compile(r"\b(?:\+1[-.\s]?)?\(?[2-9]\d{2}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    # IP address (v4) - but not version numbers like 1.2.3
    (
        "IP_ADDRESS",
        re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"),
    ),
]


class PIIScrubber:
    """PII detection and optional scrubbing.

    Detection always runs. Scrubbing (replacement) only happens
    when explicitly requested per-call via the `scrub` parameter.

    Thread-safe: stateless, uses pre-compiled patterns. Every pattern runs in
    linear time, so the whole text is always scanned: a cut-off scan
    forwarded everything past the cut unscrubbed.
    """

    def __init__(self, exclude_types: frozenset[str] | set[str] = frozenset()):
        # Operational logs exclude IP_ADDRESS (D-056): endpoint addresses
        # there are infrastructure, and redacting them made routing
        # problems undebuggable. Request and audit scrubbing keep it.
        self._patterns = [(t, p) for t, p in _PII_PATTERNS if t not in exclude_types]

    def scan(self, text: str, scrub: bool = False) -> PIIScanResult:
        """Scan text for PII and optionally scrub it.

        Args:
            text: Text to scan
            scrub: If True, produce scrubbed_text with PII replaced by placeholders

        Returns:
            PIIScanResult with detections and optionally scrubbed text
        """
        if not text:
            return PIIScanResult(has_pii=False)

        start = time.perf_counter()

        scan_text = text

        # Collect all matches with positions
        all_matches: list[PIIMatch] = []
        for pii_type, pattern in self._patterns:
            placeholder = f"[{pii_type}]"
            for m in pattern.finditer(scan_text):
                all_matches.append(
                    PIIMatch(
                        pii_type=pii_type,
                        start=m.start(),
                        end=m.end(),
                        placeholder=placeholder,
                    )
                )

        # Sort by position (for scrubbing) and remove overlaps
        all_matches.sort(key=lambda x: x.start)
        filtered: list[PIIMatch] = []
        last_end = -1
        for match in all_matches:
            if match.start >= last_end:
                filtered.append(match)
                last_end = match.end

        has_pii = len(filtered) > 0

        # Build scrubbed text if requested
        scrubbed_text = None
        if scrub and has_pii:
            parts = []
            pos = 0
            for match in filtered:
                parts.append(scan_text[pos : match.start])
                parts.append(match.placeholder)
                pos = match.end
            parts.append(scan_text[pos:])
            scrubbed_text = "".join(parts)

        elapsed = (time.perf_counter() - start) * 1000

        return PIIScanResult(
            has_pii=has_pii,
            detections=filtered,
            scrubbed_text=scrubbed_text,
            scan_time_ms=elapsed,
        )

    def scan_texts(self, texts: list, scrub: bool = False, role: str = "user") -> list[PIIFinding]:
        """Scan a batch (embedding inputs, completion prompts); non-strings skipped.

        Each finding keeps its item's index in the original list.
        """
        return [
            PIIFinding(index, role, text, self.scan(text, scrub=scrub))
            for index, text in enumerate(texts)
            if isinstance(text, str) and text
        ]

    def scan_messages(
        self, messages: list[dict], scrub: bool = False
    ) -> tuple[list[dict], list[PIIFinding]]:
        """Scan a list of chat messages for PII.

        Args:
            messages: List of message dicts with 'content' field
            scrub: If True, return messages with PII replaced

        Returns:
            Tuple of (possibly scrubbed messages, one finding per text scanned,
            located by message and content part)
        """
        results: list[PIIFinding] = []
        output_messages = []

        for msg_index, msg in enumerate(messages):
            content = msg.get("content", "")
            role = msg.get("role")
            role = getattr(role, "value", role)
            new_msg = dict(msg)  # shallow copy

            if isinstance(content, str) and content:
                result = self.scan(content, scrub=scrub)
                results.append(PIIFinding(msg_index, role, content, result))
                if scrub and result.scrubbed_text is not None:
                    new_msg["content"] = result.scrubbed_text
            elif isinstance(content, list):
                # Multimodal content arrays — scan text parts
                new_parts = []
                for part_index, part in enumerate(content):
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if isinstance(text, str) and text:
                            result = self.scan(text, scrub=scrub)
                            results.append(PIIFinding(msg_index, role, text, result, part_index))
                            if scrub and result.scrubbed_text is not None:
                                new_part = dict(part)
                                new_part["text"] = result.scrubbed_text
                                new_parts.append(new_part)
                            else:
                                new_parts.append(part)
                        else:
                            new_parts.append(part)
                    else:
                        new_parts.append(part)
                if scrub:
                    new_msg["content"] = new_parts

            # Tool-call arguments carry user-derived values too (an agent
            # replaying history sends them back verbatim). Scan every string
            # inside arguments; scrub in place, preserving the JSON shape.
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                new_calls = []
                changed = False
                for call in tool_calls:
                    if not isinstance(call, dict):
                        new_calls.append(call)
                        continue
                    function = call.get("function")
                    arguments = function.get("arguments") if isinstance(function, dict) else None
                    if not isinstance(arguments, (dict, list, str)):
                        new_calls.append(call)
                        continue
                    scrubbed_args, found = self._scan_arguments(
                        arguments, msg_index, role, results, scrub
                    )
                    if scrub and found:
                        new_call = dict(call)
                        new_call["function"] = {**function, "arguments": scrubbed_args}
                        new_calls.append(new_call)
                        changed = True
                    else:
                        new_calls.append(call)
                if scrub and changed:
                    new_msg["tool_calls"] = new_calls

            output_messages.append(new_msg)

        return output_messages, results

    def _scan_arguments(self, arguments, msg_index, role, results, scrub):
        """Scan tool-call arguments in either form (D-056).

        OpenAI sends arguments as a JSON *string*. Scanning that text
        directly missed escaped values ("jane.doe\\u0040example.com"
        decodes to an email the regex never saw) and corrupted valid JSON
        when a bare numeric token was replaced with an unquoted
        placeholder. Decode first, scan the decoded structure, and
        re-serialize only if something was scrubbed. Text that isn't
        valid JSON is scanned as plain text.
        """
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except (ValueError, TypeError):
                return self._scan_structure(arguments, msg_index, role, results, scrub)
            scrubbed, found = self._scan_structure(decoded, msg_index, role, results, scrub)
            if scrub and found:
                return json.dumps(scrubbed, ensure_ascii=False), True
            return arguments, found
        return self._scan_structure(arguments, msg_index, role, results, scrub)

    def _scan_structure(self, value, msg_index, role, results, scrub):
        """Scan every string in a JSON-like structure; return (maybe-scrubbed copy, found).

        Integers are scanned as their digit string (a phone number or SSN
        sent as a JSON number is still PII); a hit becomes a string
        placeholder, so the result stays valid JSON. Booleans are not
        numbers here.
        """
        if isinstance(value, int) and not isinstance(value, bool):
            text = str(value)
            result = self.scan(text, scrub=scrub)
            if result.has_pii:
                results.append(PIIFinding(msg_index, role, text, result))
                if scrub and result.scrubbed_text is not None:
                    return result.scrubbed_text, True
                return value, True
            return value, False
        if isinstance(value, str):
            if not value:
                return value, False
            result = self.scan(value, scrub=scrub)
            if result.has_pii:
                results.append(PIIFinding(msg_index, role, value, result))
                if scrub and result.scrubbed_text is not None:
                    return result.scrubbed_text, True
                return value, True
            return value, False
        if isinstance(value, dict):
            out = {}
            found = False
            for k, v in value.items():
                out[k], f = self._scan_structure(v, msg_index, role, results, scrub)
                found = found or f
            return out, found
        if isinstance(value, list):
            out_list = []
            found = False
            for v in value:
                item, f = self._scan_structure(v, msg_index, role, results, scrub)
                out_list.append(item)
                found = found or f
            return out_list, found
        return value, False

    def redact(self, value):
        """Return a copy of a JSON-like value with PII replaced in every string.

        For data at rest (audit bodies, stored scans): independent of the
        per-route scrub setting, so flag-only mode still never persists raw
        PII.
        """
        if isinstance(value, str):
            if not value:
                return value
            result = self.scan(value, scrub=True)
            return result.scrubbed_text if result.scrubbed_text is not None else value
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        return value
