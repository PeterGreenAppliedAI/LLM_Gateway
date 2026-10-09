"""PII extractor: an LLM finds exact values, the gateway verifies them (D-052).

The model (phi4-mini by default) is asked for {category, value} pairs under
a grammar-constrained schema. Every value must appear verbatim in the text;
anything that doesn't is discarded and counted, so a hallucinated "PII"
value can never alter a prompt or enter the dataset.

Calls go straight to the Ollama endpoint, never through the gateway's own
routes: those write audit bodies redacted only by the regex scrubber,
which misses exactly what this pipeline exists to catch.
"""

import ipaddress
import json
import re
import time
from dataclasses import dataclass, field

import httpx

from gateway.security.pii_taxonomy import FINDER_SCHEMA, LABELS, finder_system_prompt

_PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*>|\[[A-Z_ ]+\]|\{[a-z_]+\}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_MAC = re.compile(r"[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}")
_URL = re.compile(r"https?://|www\.")
_ORG_SUFFIX = re.compile(r"\b(?:LLC|Inc|Ltd|Corp|GmbH|Co|Company|Services|Group)\b\.?$")
# Words that make a "name" a business, not a person
_ORG_WORD = re.compile(
    r"\b(?:Electric(?:al)?|Auto|Glass|Body|Storage|Quarters|Plumbing|Roofing|Foundation|"
    r"Cent(?:er|re)|Council|Associates|Partners|Solutions|Systems|Technologies|Industries|"
    r"Enterprises|Restaurant|Cafe|Shop|Store|Market|Clinic|Hospital|School|University|"
    r"Office|Bank|Agency|Studio|Labs?|Construction|Contracting|Repair|Removal|Towing)\b",
    re.I,
)
_PLACEHOLDER_NAME = {"unknown", "user", "admin", "anonymous", "n/a", "none", "null", "customer"}
_DATE = re.compile(r"\(?\d{4}-\d{2}-\d{2}\)?|\(?\d{1,2}/\d{1,2}/\d{2,4}\)?")
_KNOWN_KEY_PREFIX = re.compile(
    r"^(?:sk-|sk_live_|rk_live_|AKIA|ASIA|ghp_|gho_|ghs_|github_pat_|xox[abpr]-|AIza|gw-|admin-|eyJ)"
)


_DIGIT_RUN = re.compile(r"\+?\(?[A-Za-z]{0,4}\d[\w().:/-]*(?:[ ]\(?\d[\w().:/-]*)*")
# Taxonomy hard negatives that a pattern can recognise (D-052 table)
_TEST_CARDS = {
    "4111111111111111",
    "4242424242424242",
    "5555555555554444",
    "378282246310005",
    "4000056655665556",
    "5105105105105100",
    "6011111111111117",
}
_DOC_EMAIL = re.compile(
    r"@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|[\w-]+\.(?:test|invalid|example))$", re.I
)
_FICTIONAL_PHONE = re.compile(r"555[\s.-]?01\d\d\b")
_LOG_TEXT = re.compile(
    r"\b(?:error|failed|failure|exception|timeout|traceback|warn(?:ing)?|denied|refused|"
    r"not found|no space|device|disk)\b",
    re.I,
)
_TOKEN_CATEGORIES = ("CREDENTIAL", "GOV_ID", "FINANCIAL", "NETWORK")


def core_value(category: str, value: str) -> str:
    """Strip a label the model copied along with the value ("IBAN DE89...",
    "card 4242 ...") for categories whose values are a single token or one run
    of digit groups. Free-text categories are returned unchanged."""
    v = value.strip()
    if category not in _TOKEN_CATEGORIES or " " not in v:
        return v
    if category == "CREDENTIAL":
        return max(v.split(), key=len)
    runs = _DIGIT_RUN.findall(v)
    return max(runs, key=len).strip() if runs else v


def _digits(value: str) -> int:
    return sum(ch.isdigit() for ch in value)


def plausible(category: str, value: str) -> bool:
    """Cheap shape check on an extracted value (D-052).

    The small extractor over-reports: placeholders, field labels, log
    fragments. A value that can't be the category it claims is rejected
    before it can alter a prompt or enter the dataset. Deliberately loose:
    it only rules out the impossible, it doesn't confirm.
    """
    v = value.strip()
    if len(v) < 3 or _PLACEHOLDER.search(v):
        return False
    if category == "CREDENTIAL":
        if v.startswith("-----BEGIN") or "://" in v and "@" in v:
            return True  # PEM block, connection string with credentials
        if any(c.isspace() for c in v) or len(v) < 8:
            return False
        if _KNOWN_KEY_PREFIX.match(v):
            return True
        # Unprefixed secret: long, letters and digits (rules out "TXN-1043", "LASER-WL")
        return len(v) >= 12 and any(c.isalpha() for c in v) and _digits(v) >= 2
    if category == "NETWORK":
        if _IPV4.fullmatch(v):
            try:
                return ipaddress.ip_address(v).is_global
            except ValueError:
                return False
        if _MAC.fullmatch(v):
            return True
        try:
            return ipaddress.ip_address(v).is_global  # IPv6
        except ValueError:
            return len(v) >= 12 and " " not in v and _digits(v) >= 4  # device/ad IDs
    if category == "CONTACT":
        if _DOC_EMAIL.search(v) or _FICTIONAL_PHONE.search(v):
            return False
        return (
            bool(_EMAIL.fullmatch(v))
            or _digits(v) >= 7
            or (_digits(v) >= 1 and len(v.split()) >= 3)
        )  # postal address
    if category in ("GOV_ID", "FINANCIAL"):
        if "".join(c for c in v if c.isdigit()) in _TEST_CARDS or _DATE.fullmatch(v):
            return False
        if category == "FINANCIAL":
            return _digits(v) >= 8  # cards, account numbers, IBANs; not short hex ids
        return _digits(v) >= 5
    if category == "PERSON_NAME":
        if _ORG_SUFFIX.search(v) or _ORG_WORD.search(v) or v.lower() in _PLACEHOLDER_NAME:
            return False
        return (
            _digits(v) == 0
            and len(v.split()) <= 6
            and any(c.isupper() for c in v)
            and not _URL.search(v)
        )
    # HEALTH, QUASI_ID, SPECIAL are free text; rule out what is clearly not
    if v.startswith("*") or (
        category != "HEALTH" and v.isupper() and len(v) <= 12 and not _digits(v)
    ):
        return False  # markdown emphasis, status words ("**HIGH**", "OTR/L")
    return len(v) <= 200 and not _URL.search(v) and not _LOG_TEXT.search(v)


@dataclass(frozen=True)
class PIISpan:
    category: str
    value: str
    start: int
    end: int


@dataclass
class FinderResult:
    spans: list[PIISpan] = field(default_factory=list)
    hallucinated: int = 0  # values the model returned that aren't in the text
    rejected: int = 0  # values in the text that can't be the category claimed
    error: str | None = None
    latency_ms: float = 0.0

    @property
    def categories(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for span in self.spans:
            counts[span.category] = counts.get(span.category, 0) + 1
        return counts


def locate_spans(text: str, findings: list[dict]) -> tuple[list[PIISpan], int, int]:
    """Turn model findings into verified spans.

    Every occurrence of a verified value becomes a span (a value repeated in
    the text must be scrubbed everywhere). Unknown categories, empty values
    and values absent from the text are dropped; absences are counted as
    hallucinations, implausible values (see `plausible`) as rejections.
    Returns (spans, hallucinated, rejected).
    """
    spans: list[PIISpan] = []
    seen: set[tuple[str, str]] = set()
    hallucinated = rejected = 0
    for item in findings:
        if not isinstance(item, dict):
            continue
        category, value = item.get("category"), item.get("value")
        if category not in LABELS or not isinstance(value, str) or not value.strip():
            continue
        if (category, value) in seen:
            continue
        seen.add((category, value))
        start = text.find(value)
        if start < 0 or category in _TOKEN_CATEGORIES:
            core = core_value(category, value)
            if core != value and text.find(core) >= 0:
                value, start = core, text.find(core)
        if start < 0:
            hallucinated += 1
            continue
        if not plausible(category, value):
            rejected += 1
            continue
        while start >= 0:
            spans.append(PIISpan(category, value, start, start + len(value)))
            start = text.find(value, start + len(value))
    spans.sort(key=lambda s: (s.start, s.end))
    return spans, hallucinated, rejected


class PIIFinder:
    """Extract PII values from text with an Ollama-served model."""

    def __init__(
        self,
        base_url: str,
        model: str = "phi4-mini",
        timeout: float = 60.0,
        max_output_tokens: int = 1024,
        client: httpx.AsyncClient | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._timeout = timeout
        self._max_output_tokens = max_output_tokens
        self._client = client
        self._system = finder_system_prompt()

    async def find(self, text: str) -> FinderResult:
        started = time.perf_counter()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system},
                {"role": "user", "content": text},
            ],
            "format": FINDER_SCHEMA,
            "stream": False,
            "options": {"temperature": 0, "num_predict": self._max_output_tokens},
        }
        try:
            client = self._client or httpx.AsyncClient(timeout=self._timeout)
            try:
                response = await client.post(f"{self._base_url}/api/chat", json=payload)
            finally:
                if self._client is None:
                    await client.aclose()
            response.raise_for_status()
            content = response.json().get("message", {}).get("content", "")
            findings = json.loads(content).get("findings", [])
            if not isinstance(findings, list):
                raise ValueError("findings is not a list")
        except Exception as e:
            return FinderResult(
                error=f"{type(e).__name__}: {e}"[:300],
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        spans, hallucinated, rejected = locate_spans(text, findings)
        return FinderResult(
            spans=spans,
            hallucinated=hallucinated,
            rejected=rejected,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
