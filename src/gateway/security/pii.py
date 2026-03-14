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


# Pre-compiled PII patterns
# Order matters — more specific patterns first to avoid partial matches
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    # SSN: 123-45-6789 or 123 45 6789 (but NOT 9 digits with no separators to reduce false positives)
    ("SSN", re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b")),
    # Credit card: 4 groups of 4 digits, with optional separators
    ("CREDIT_CARD", re.compile(r"\b(?:\d{4}[-\s]){3}\d{4}\b")),
    # Email
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
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

    Thread-safe: stateless, uses pre-compiled patterns.
    """

    def __init__(self, max_input_length: int = 100_000):
        """Initialize PII scrubber.

        Args:
            max_input_length: Maximum text length to scan (truncates for safety)
        """
        self._max_input_length = max_input_length

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

        # Truncate for safety
        scan_text = text[: self._max_input_length]

        # Collect all matches with positions
        all_matches: list[PIIMatch] = []
        for pii_type, pattern in _PII_PATTERNS:
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

    def scan_messages(
        self, messages: list[dict], scrub: bool = False
    ) -> tuple[list[dict], list[PIIScanResult]]:
        """Scan a list of chat messages for PII.

        Args:
            messages: List of message dicts with 'content' field
            scrub: If True, return messages with PII replaced

        Returns:
            Tuple of (possibly scrubbed messages, list of scan results)
        """
        results: list[PIIScanResult] = []
        output_messages = []

        for msg in messages:
            content = msg.get("content", "")
            new_msg = dict(msg)  # shallow copy

            if isinstance(content, str) and content:
                result = self.scan(content, scrub=scrub)
                results.append(result)
                if scrub and result.scrubbed_text is not None:
                    new_msg["content"] = result.scrubbed_text
            elif isinstance(content, list):
                # Multimodal content arrays — scan text parts
                new_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        if isinstance(text, str) and text:
                            result = self.scan(text, scrub=scrub)
                            results.append(result)
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

            output_messages.append(new_msg)

        return output_messages, results
