"""Unicode sanitization for prompt injection defense.

Strips dangerous invisible characters that can be used to hide
malicious instructions in seemingly innocent text.

Zero latency overhead - pure string operations.
"""

import unicodedata
from dataclasses import dataclass, field
from enum import Enum


class SanitizationType(str, Enum):
    """Types of sanitization performed."""

    ZERO_WIDTH = "zero_width"
    DIRECTIONAL = "directional"
    CONTROL_CHAR = "control_char"
    HOMOGLYPH = "homoglyph"
    ENCODING = "encoding"


@dataclass
class SanitizationResult:
    """Result of sanitization operation."""

    original: str
    sanitized: str
    modified: bool
    removals: dict[SanitizationType, int] = field(default_factory=dict)

    @property
    def total_removals(self) -> int:
        return sum(self.removals.values())

    def to_dict(self) -> dict:
        return {
            "modified": self.modified,
            "total_removals": self.total_removals,
            "removals": {k.value: v for k, v in self.removals.items()},
        }


class Sanitizer:
    """Unicode sanitizer for removing dangerous invisible characters.

    Removes:
    - Zero-width characters (used to hide text)
    - Directional override characters (used to reverse text display)
    - Control characters (non-printable manipulation)
    - BOM and other format characters

    Does NOT remove:
    - Normal whitespace (space, tab, newline)
    - Legitimate Unicode (accented chars, emoji, CJK, etc.)
    """

    # Zero-width characters - can hide text between visible characters
    ZERO_WIDTH_CHARS = {
        "\u200b",  # Zero-width space
        "\u200c",  # Zero-width non-joiner
        "\u200d",  # Zero-width joiner
        "\u2060",  # Word joiner
        "\u2061",  # Function application
        "\u2062",  # Invisible times
        "\u2063",  # Invisible separator
        "\u2064",  # Invisible plus
        "\ufeff",  # BOM / Zero-width no-break space
    }

    # Directional override - can reverse text display
    DIRECTIONAL_CHARS = {
        "\u200e",  # Left-to-right mark
        "\u200f",  # Right-to-left mark
        "\u202a",  # Left-to-right embedding
        "\u202b",  # Right-to-left embedding
        "\u202c",  # Pop directional formatting
        "\u202d",  # Left-to-right override
        "\u202e",  # Right-to-left override
        "\u2066",  # Left-to-right isolate
        "\u2067",  # Right-to-left isolate
        "\u2068",  # First strong isolate
        "\u2069",  # Pop directional isolate
    }

    # Control characters (except tab, newline, carriage return)
    ALLOWED_CONTROL = {"\t", "\n", "\r"}

    # Tags block - deprecated Unicode block sometimes used for hiding
    TAGS_RANGE = range(0xE0000, 0xE007F + 1)

    # Variation selectors - can alter character appearance
    VARIATION_SELECTORS = set(range(0xFE00, 0xFE0F + 1)) | set(range(0xE0100, 0xE01EF + 1))

    def __init__(
        self,
        remove_zero_width: bool = True,
        remove_directional: bool = True,
        remove_control: bool = True,
        remove_tags: bool = True,
        normalize_unicode: bool = True,
    ):
        """Initialize sanitizer with configurable options.

        Args:
            remove_zero_width: Remove zero-width characters
            remove_directional: Remove directional override characters
            remove_control: Remove control characters (except whitespace)
            remove_tags: Remove Unicode tags block
            normalize_unicode: Normalize to NFC form
        """
        self.remove_zero_width = remove_zero_width
        self.remove_directional = remove_directional
        self.remove_control = remove_control
        self.remove_tags = remove_tags
        self.normalize_unicode = normalize_unicode

        # Pre-compile removal set for performance
        self._build_removal_set()

    def _build_removal_set(self) -> None:
        """Build the set of characters to remove."""
        self._removal_chars: set[str] = set()

        if self.remove_zero_width:
            self._removal_chars.update(self.ZERO_WIDTH_CHARS)
        if self.remove_directional:
            self._removal_chars.update(self.DIRECTIONAL_CHARS)
        if self.remove_tags:
            self._removal_chars.update(chr(c) for c in self.TAGS_RANGE)

    def sanitize(self, text: str) -> SanitizationResult:
        """Sanitize text by removing dangerous invisible characters.

        Args:
            text: Input text to sanitize

        Returns:
            SanitizationResult with sanitized text and removal counts
        """
        if not text:
            return SanitizationResult(
                original=text,
                sanitized=text,
                modified=False,
            )

        removals: dict[SanitizationType, int] = {}
        result_chars: list[str] = []

        for char in text:
            # Check zero-width
            if self.remove_zero_width and char in self.ZERO_WIDTH_CHARS:
                removals[SanitizationType.ZERO_WIDTH] = (
                    removals.get(SanitizationType.ZERO_WIDTH, 0) + 1
                )
                continue

            # Check directional
            if self.remove_directional and char in self.DIRECTIONAL_CHARS:
                removals[SanitizationType.DIRECTIONAL] = (
                    removals.get(SanitizationType.DIRECTIONAL, 0) + 1
                )
                continue

            # Check control characters
            if self.remove_control:
                if unicodedata.category(char) == "Cc" and char not in self.ALLOWED_CONTROL:
                    removals[SanitizationType.CONTROL_CHAR] = (
                        removals.get(SanitizationType.CONTROL_CHAR, 0) + 1
                    )
                    continue

            # Check tags block
            if self.remove_tags and ord(char) in self.TAGS_RANGE:
                removals[SanitizationType.CONTROL_CHAR] = (
                    removals.get(SanitizationType.CONTROL_CHAR, 0) + 1
                )
                continue

            result_chars.append(char)

        sanitized = "".join(result_chars)

        # Normalize Unicode if enabled
        if self.normalize_unicode:
            sanitized = unicodedata.normalize("NFC", sanitized)

        return SanitizationResult(
            original=text,
            sanitized=sanitized,
            modified=sanitized != text,
            removals=removals,
        )

    def sanitize_messages(
        self,
        messages: list[dict],
    ) -> tuple[list[dict], list[SanitizationResult]]:
        """Sanitize a list of chat messages.

        Args:
            messages: List of message dicts with 'content' field

        Returns:
            Tuple of (sanitized messages, list of results)
        """
        results: list[SanitizationResult] = []
        sanitized_messages: list[dict] = []

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                result = self.sanitize(content)
                results.append(result)
                sanitized_msg = {**msg, "content": result.sanitized}
            else:
                # Handle non-string content (e.g., multimodal)
                sanitized_msg = msg
            sanitized_messages.append(sanitized_msg)

        return sanitized_messages, results


# Default sanitizer instance
default_sanitizer = Sanitizer()


def sanitize(text: str) -> SanitizationResult:
    """Convenience function using default sanitizer."""
    return default_sanitizer.sanitize(text)
