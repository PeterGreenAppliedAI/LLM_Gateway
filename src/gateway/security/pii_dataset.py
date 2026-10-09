"""Training data for the PII gate and extractor (D-052).

Two sources, one record format (JSONL, one object per line):

    {"id": "<sha256 prefix of text>", "text": "...",
     "spans": [{"category": "CONTACT", "value": "...", "start": 6, "end": 23}],
     "source": "backlog" | "synthetic", "labeler": "phi4-mini" | "generator",
     "hallucinated": 0}

- **Synthetic:** fake values for every taxonomy category, placed in varied
  sentences, so spans are exact by construction; plus hard negatives
  (placeholder keys, test cards, example.com, internal IPs) labelled clean.
- **Backlog:** unique texts from the pre-redaction security-scan corpus,
  labelled by the extractor, keeping only spans verified verbatim.

Dataset files contain raw text by design: written under data/ (gitignored)
with owner-only permissions. Never commit or export them.

    python -m gateway.security.pii_dataset synthetic --count 5000
    python -m gateway.security.pii_dataset label-backlog --limit 20000 \\
        --finder-url http://10.0.0.14:11434
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import string
import sys
from collections.abc import Iterator
from pathlib import Path

from gateway.security.pii_finder import FinderResult, PIIFinder
from gateway.security.pii_shadow import messages_to_text

DEFAULT_DIR = Path("data/pii-dataset")

# Text the gateway's own redaction produced: such rows describe placeholders,
# not PII, and would teach the gate to detect "[EMAIL]" (D-041/D-052)
REDACTION_MARKERS = (
    "[EMAIL]",
    "[PHONE]",
    "[SSN]",
    "[CREDIT_CARD]",
    "[IP_ADDRESS]",
    "[REDACTION FAILED]",
    "[messages not stored",
)

# Upstream log templating (the log pipeline's own placeholders): the values
# are already gone, and the extractor labels the placeholders themselves
TEMPLATE_PLACEHOLDER = re.compile(r"<(?:IPV4|IPV6|N|NUM|HEX|UUID|HASH|PATH|URL|EMAIL)>")
_SHAPE_DIGITS = re.compile(r"\d+")
_SHAPE_HEX = re.compile(r"\b[0-9a-f]{8,}\b")


def text_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def shape_id(text: str) -> str:
    """Near-duplicate key: the same log line with different counters is one shape."""
    shape = _SHAPE_DIGITS.sub("0", _SHAPE_HEX.sub("h", text))
    return hashlib.sha256(shape.encode()).hexdigest()[:16]


def make_record(
    text: str,
    spans: list[dict],
    source: str,
    labeler: str,
    hallucinated: int = 0,
    rejected: int = 0,
) -> dict:
    return {
        "id": text_id(text),
        "shape": shape_id(text),
        "text": text,
        "spans": spans,
        "source": source,
        "labeler": labeler,
        "hallucinated": hallucinated,
        "rejected": rejected,
    }


def record_from_finder(text: str, result: FinderResult, labeler: str = "phi4-mini") -> dict:
    spans = [
        {"category": s.category, "value": s.value, "start": s.start, "end": s.end}
        for s in result.spans
    ]
    return make_record(text, spans, "backlog", labeler, result.hallucinated, result.rejected)


# =============================================================================
# Synthetic generation
# =============================================================================

FIRST = [
    "Maria",
    "James",
    "Aisha",
    "Wei",
    "Olga",
    "Daniel",
    "Priya",
    "Tomás",
    "Fatima",
    "Liam",
    "Sofia",
    "Kwame",
    "Hannah",
    "Mateo",
    "Yuki",
    "Noah",
    "Elena",
    "Omar",
    "Grace",
    "Lucas",
]
LAST = [
    "Gonzalez",
    "Okafor",
    "Chen",
    "Novak",
    "Patel",
    "Müller",
    "Haddad",
    "Larsen",
    "Kim",
    "Silva",
    "Brennan",
    "Ivanova",
    "Nakamura",
    "Mensah",
    "Rossi",
    "Dubois",
    "Kowalski",
]
DOMAINS = [
    "acme-corp.com",
    "northwind.io",
    "fabrikam.net",
    "contoso-mail.com",
    "globex.org",
    "initech.co",
    "umbrella-labs.com",
    "gmail.com",
    "outlook.com",
    "proton.me",
]
STREETS = ["Maple Ave", "Oak Street", "Harbor Rd", "Elm Court", "Sunset Blvd", "Mill Lane"]
CITIES = [
    ("Atlanta", "GA", "30318"),
    ("Denver", "CO", "80205"),
    ("Austin", "TX", "78704"),
    ("Portland", "OR", "97214"),
    ("Columbus", "OH", "43215"),
]
CONDITIONS = [
    "type 2 diabetes",
    "major depressive disorder",
    "stage II breast cancer",
    "chronic kidney disease",
    "HIV",
    "bipolar disorder",
    "epilepsy",
]
SPECIAL_ATTRS = [
    "a practicing Muslim",
    "gay",
    "a registered Republican",
    "a union shop steward",
    "convicted of a felony in 2015",
    "a Jehovah's Witness",
    "transgender",
]
ROLES = ["the only night-shift pharmacist", "the CFO", "the lead engineer", "a junior paralegal"]
EMPLOYERS = ["Northwind Logistics", "Fabrikam Health", "Contoso Bank", "Globex Retail"]


def _rand(rng: random.Random, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def _luhn_complete(prefix: str) -> str:
    """Append the Luhn check digit."""
    total = 0
    for i, d in enumerate(reversed(prefix)):
        n = int(d)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return prefix + str((10 - total % 10) % 10)


class SyntheticGenerator:
    """Fake PII in varied sentences, with exact spans by construction."""

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)

    # --- values ------------------------------------------------------------
    def _name(self) -> str:
        return f"{self.rng.choice(FIRST)} {self.rng.choice(LAST)}"

    def credential(self) -> str:
        r, a = self.rng, string.ascii_letters + string.digits
        return r.choice(
            [
                lambda: "sk-ant-api03-" + _rand(r, a + "-_", 40),
                lambda: "sk-proj-" + _rand(r, a, 48),
                lambda: "AKIA" + _rand(r, string.ascii_uppercase + string.digits, 16),
                lambda: "ghp_" + _rand(r, a, 36),
                lambda: (
                    "xoxb-"
                    + _rand(r, string.digits, 12)
                    + "-"
                    + _rand(r, string.digits, 12)
                    + "-"
                    + _rand(r, a, 24)
                ),
                lambda: "AIza" + _rand(r, a + "-_", 35),
                lambda: "sk_live_" + _rand(r, a, 24),
                lambda: "gw-" + _rand(r, a + "-_", 43),
                lambda: (
                    "eyJhbGciOiJIUzI1NiJ9." + _rand(r, a + "-_", 40) + "." + _rand(r, a + "-_", 43)
                ),
                lambda: (
                    f"postgres://svc_{_rand(r, string.ascii_lowercase, 5)}:{_rand(r, a, 14)}@db.internal:5432/prod"
                ),
            ]
        )()

    def ssn(self) -> str:
        area = self.rng.choice([n for n in range(1, 900) if n != 666])
        return f"{area:03d}-{self.rng.randint(1, 99):02d}-{self.rng.randint(1, 9999):04d}"

    def card(self) -> str:
        prefix = self.rng.choice(["4", "51", "52", "37"])
        body = prefix + _rand(
            self.rng, string.digits, (15 if prefix == "37" else 16) - len(prefix) - 1
        )
        number = _luhn_complete(body)
        if self.rng.random() < 0.5 and len(number) == 16:
            return " ".join(number[i : i + 4] for i in range(0, 16, 4))
        return number

    def email(self) -> str:
        first, last = self.rng.choice(FIRST).lower(), self.rng.choice(LAST).lower()
        first = first.encode("ascii", "ignore").decode() or "user"
        last = last.encode("ascii", "ignore").decode() or "name"
        return f"{first}.{last}@{self.rng.choice(DOMAINS)}"

    def phone(self) -> str:
        r = self.rng
        return r.choice(
            [
                lambda: f"({r.randint(201, 989)}) {r.randint(200, 999)}-{r.randint(1000, 9999)}",
                lambda: f"+44 20 {r.randint(1000, 9999)} {r.randint(1000, 9999)}",
                lambda: f"+49 30 {r.randint(10000000, 99999999)}",
                lambda: f"{r.randint(201, 989)}.{r.randint(200, 999)}.{r.randint(1000, 9999)}",
            ]
        )()

    def address(self) -> str:
        city, state, zipcode = self.rng.choice(CITIES)
        return f"{self.rng.randint(12, 9876)} {self.rng.choice(STREETS)}, {city}, {state} {zipcode}"

    def public_ip(self) -> str:
        while True:
            octets = [
                self.rng.randint(1, 223),
                self.rng.randint(0, 255),
                self.rng.randint(0, 255),
                self.rng.randint(1, 254),
            ]
            if (
                octets[0] in (10, 127)
                or (octets[0] == 172 and 16 <= octets[1] <= 31)
                or (octets[0] == 192 and octets[1] == 168)
            ):
                continue
            return ".".join(map(str, octets))

    def mac(self) -> str:
        return ":".join(_rand(self.rng, "0123456789abcdef", 2) for _ in range(6))

    # --- sentences ---------------------------------------------------------
    def _compose(self, parts: list) -> tuple[str, list[dict]]:
        """parts: str (plain) or (category, value). Returns text and exact spans."""
        text, spans = "", []
        for part in parts:
            if isinstance(part, tuple):
                category, value = part
                spans.append(
                    {
                        "category": category,
                        "value": value,
                        "start": len(text),
                        "end": len(text) + len(value),
                    }
                )
                text += value
            else:
                text += part
        return text, spans

    def positive(self) -> dict:
        name = self._name()
        templates = [
            lambda: [
                "Here's the key for the staging deploy: ",
                ("CREDENTIAL", self.credential()),
                " — rotate it after Friday.",
            ],
            lambda: [
                "Set OPENAI_API_KEY=",
                ("CREDENTIAL", self.credential()),
                " in the .env and restart.",
            ],
            lambda: [
                "Customer ",
                ("PERSON_NAME", name),
                " (SSN ",
                ("GOV_ID", self.ssn()),
                ") disputed the charge.",
            ],
            lambda: ["Refund to card ", ("FINANCIAL", self.card()), " for order 88231."],
            lambda: [
                "Patient ",
                ("PERSON_NAME", name),
                ", MRN ",
                ("HEALTH", f"MRN-{self.rng.randint(1000000, 9999999)}"),
                ", was diagnosed with ",
                ("HEALTH", self.rng.choice(CONDITIONS)),
                ".",
            ],
            lambda: [
                "Please reach ",
                ("PERSON_NAME", name),
                " at ",
                ("CONTACT", self.email()),
                " or ",
                ("CONTACT", self.phone()),
                ".",
            ],
            lambda: [
                "Ship it to ",
                ("CONTACT", self.address()),
                ", attention ",
                ("PERSON_NAME", name),
                ".",
            ],
            lambda: [
                ("PERSON_NAME", name),
                " was born on ",
                (
                    "QUASI_ID",
                    f"{self.rng.randint(1950, 2005)}-{self.rng.randint(1, 12):02d}-{self.rng.randint(1, 28):02d}",
                ),
                " and is ",
                ("QUASI_ID", self.rng.choice(ROLES) + " at " + self.rng.choice(EMPLOYERS)),
                ".",
            ],
            lambda: [
                "Between us, ",
                ("PERSON_NAME", name),
                " is ",
                ("SPECIAL", self.rng.choice(SPECIAL_ATTRS)),
                ", so keep that out of the report.",
            ],
            lambda: [
                "Login spike from ",
                ("NETWORK", self.public_ip()),
                " (device ",
                ("NETWORK", self.mac()),
                ") at 03:12 UTC.",
            ],
            lambda: [
                "Wire it to IBAN ",
                ("FINANCIAL", "DE" + _rand(self.rng, string.digits, 20)),
                " and confirm with ",
                ("CONTACT", self.email()),
                ".",
            ],
        ]
        text, spans = self._compose(self.rng.choice(templates)())
        return make_record(text, spans, "synthetic", "generator")

    def hard_negative(self) -> dict:
        """Near-misses the gate must learn to call clean."""
        r = self.rng
        text = r.choice(
            [
                lambda: (
                    "Set OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx in your .env (replace with your key)."
                ),
                lambda: "Add `Authorization: Bearer <TOKEN>` and set api_key='your-api-key-here'.",
                lambda: (
                    f"Request {_rand(r, '0123456789abcdef', 8)}-{_rand(r, '0123456789abcdef', 4)}-4{_rand(r, '0123456789abcdef', 3)}-a{_rand(r, '0123456789abcdef', 3)}-{_rand(r, '0123456789abcdef', 12)} timed out."
                ),
                lambda: f"Fixed in commit {_rand(r, '0123456789abcdef', 40)}.",
                lambda: "Use the Stripe test card 4111 1111 1111 1111 with any future date.",
                lambda: "Send a sample to test@example.com to check the template.",
                lambda: "Call the demo line at 555-0142 to hear the IVR menu.",
                lambda: (
                    f"The A5000 node is at 10.0.0.{r.randint(2, 250)} and the Mac at 192.168.1.{r.randint(2, 250)}."
                ),
                lambda: (
                    f"Upgraded the library to version {r.randint(1, 9)}.{r.randint(0, 20)}.{r.randint(0, 9)}.{r.randint(0, 99)} last week."
                ),
                lambda: "Abraham Lincoln delivered the Gettysburg Address in 1863.",
                lambda: "Diabetes affects about 11% of US adults, according to the CDC.",
                lambda: "Total due: $4,210.33 on invoice 2026-0418, net 30.",
                lambda: "Sherlock Holmes lives at 221B Baker Street in the stories.",
            ]
        )()
        return make_record(text, [], "synthetic", "generator")

    def generate(self, count: int, negative_share: float = 0.3) -> Iterator[dict]:
        for _ in range(count):
            yield self.hard_negative() if self.rng.random() < negative_share else self.positive()


# =============================================================================
# Backlog labelling
# =============================================================================


def usable_backlog_text(messages) -> str | None:
    """Text worth labelling from a stored scan, or None (redacted/placeholder/empty)."""
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except ValueError:
            return None
    if not isinstance(messages, list):
        return None
    text = messages_to_text(messages).strip()
    if not text or any(marker in text for marker in REDACTION_MARKERS):
        return None
    if TEMPLATE_PLACEHOLDER.search(text):
        return None
    return text


def _open_private(path: Path, mode: str = "a"):
    """Dataset files hold raw text: owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC), 0o600
    )
    os.chmod(path, 0o600)
    return os.fdopen(fd, mode, encoding="utf-8")


def existing_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    return {r["id"] for r in records} | {r["shape"] for r in records if "shape" in r}


async def label_backlog(
    db_url: str,
    out: Path,
    finder: PIIFinder,
    limit: int,
    concurrency: int = 4,
    order: str = "random",
    log=print,
) -> int:
    """Label up to `limit` new unique backlog texts; resumable (skips ids already in `out`)."""
    from sqlalchemy import func, select

    from gateway.storage import DatabaseConfig, create_async_db_engine
    from gateway.storage.schema import security_scans

    done = existing_ids(out)
    engine = await create_async_db_engine(DatabaseConfig(url=db_url), create_tables=False)
    # Order ids only, then fetch bodies in batches: sorting the message JSON
    # itself would drag the whole corpus through the sort
    ids_stmt = select(security_scans.c.id).order_by(
        func.random() if order == "random" else security_scans.c.id.desc()
    )

    seen: set[str] = set(done)
    queue: list[str] = []
    try:
        async with engine.connect() as conn:
            ids = [row[0] for row in (await conn.execute(ids_stmt)).all()]
            for i in range(0, len(ids), 500):
                if len(queue) >= limit:
                    break
                batch = ids[i : i + 500]
                rows = await conn.execute(
                    select(security_scans.c.messages).where(security_scans.c.id.in_(batch))
                )
                for (messages,) in rows:
                    text = usable_backlog_text(messages)
                    if text is None:
                        continue
                    tid, sid = text_id(text), shape_id(text)
                    if tid in seen or sid in seen:
                        continue
                    seen.update((tid, sid))
                    queue.append(text)
                    if len(queue) >= limit:
                        break
    finally:
        await engine.dispose()
    log(f"{len(queue)} new unique texts to label ({len(done)} already in {out})")

    sem = asyncio.Semaphore(concurrency)
    written = errors = 0
    with _open_private(out) as f:

        async def one(text: str) -> None:
            nonlocal written, errors
            async with sem:
                result = await finder.find(text)
            if result.error:
                errors += 1
                return
            f.write(
                json.dumps(record_from_finder(text, result, finder.model), ensure_ascii=False)
                + "\n"
            )
            written += 1
            if written % 100 == 0:
                f.flush()
                log(f"  labelled {written}/{len(queue)} ({errors} errors)")

        await asyncio.gather(*(one(t) for t in queue))
    log(f"done: {written} labelled, {errors} extractor errors -> {out}")
    return written


def refilter(src: Path, out: Path) -> tuple[int, int]:
    """Re-apply the current shape checks to labelled records, so tightening
    `plausible` never needs a relabelling pass. Returns (records, spans dropped)."""
    from gateway.security.pii_finder import plausible

    records = dropped = 0
    with src.open(encoding="utf-8") as f, _open_private(out, mode="w") as w:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            kept = [sp for sp in record["spans"] if plausible(sp["category"], sp["value"])]
            dropped += len(record["spans"]) - len(kept)
            record["spans"] = kept
            w.write(json.dumps(record, ensure_ascii=False) + "\n")
            records += 1
    return records, dropped


def write_synthetic(out: Path, count: int, seed: int, negative_share: float) -> int:
    n = 0
    with _open_private(out, mode="w") as f:
        for record in SyntheticGenerator(seed).generate(count, negative_share):
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gateway.security.pii_dataset")
    sub = parser.add_subparsers(dest="command", required=True)

    syn = sub.add_parser("synthetic", help="generate synthetic labelled examples")
    syn.add_argument("--count", type=int, default=5000)
    syn.add_argument("--seed", type=int, default=0)
    syn.add_argument("--negative-share", type=float, default=0.3)
    syn.add_argument("--out", type=Path, default=DEFAULT_DIR / "synthetic.jsonl")

    lab = sub.add_parser("label-backlog", help="label stored scans with the extractor")
    lab.add_argument("--limit", type=int, default=20000)
    lab.add_argument("--concurrency", type=int, default=4)
    lab.add_argument("--order", choices=["random", "newest"], default="random")
    lab.add_argument(
        "--db-url", default=os.environ.get("GATEWAY_DB_URL", "sqlite:///data/gateway.db")
    )
    lab.add_argument("--finder-url", default="http://localhost:11434")
    lab.add_argument("--finder-model", default="phi4-mini")
    lab.add_argument("--out", type=Path, default=DEFAULT_DIR / "backlog.jsonl")

    ref = sub.add_parser("refilter", help="re-apply current value checks to a labelled file")
    ref.add_argument("src", type=Path)
    ref.add_argument("out", type=Path)

    args = parser.parse_args(argv)
    if args.command == "refilter":
        n, dropped = refilter(args.src, args.out)
        print(f"{n} records, {dropped} spans dropped -> {args.out}")
        return 0
    if args.command == "synthetic":
        n = write_synthetic(args.out, args.count, args.seed, args.negative_share)
        print(f"{n} synthetic records -> {args.out}")
        return 0
    finder = PIIFinder(args.finder_url, model=args.finder_model)
    asyncio.run(
        label_backlog(args.db_url, args.out, finder, args.limit, args.concurrency, args.order)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
