"""The PII taxonomy (D-052): one source for every consumer.

The extractor prompt, the gate's questions, the extractor's JSON schema,
the labelling job and (later) enforcement policy all derive from this
module, so a category can't exist in one place and not another. The
definitions mirror the table in docs/DECISIONS.md (D-052 taxonomy); change
both together.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PIICategory:
    label: str
    name: str
    includes: str
    hard_negatives: str
    # The yes/no question the gate (Laya) answers for this category
    question: str


CATEGORIES: tuple[PIICategory, ...] = (
    PIICategory(
        label="CREDENTIAL",
        name="Credentials and secrets",
        includes=(
            "API keys (sk-..., sk-ant-..., AKIA..., ghp_..., xoxb-/xoxp-..., AIza..., "
            "sk_live_..., gw-..., admin-...), bearer tokens, JWTs, passwords given as "
            "values, private keys (PEM blocks), connection strings with credentials, "
            "webhook URLs with embedded secrets"
        ),
        hard_negatives=(
            "placeholders (sk-xxxx, your-api-key-here, <TOKEN>), redacted values, "
            "UUIDs and request IDs, commit hashes, sk_test_ keys in documentation"
        ),
        question="Does the text contain a real credential or secret, such as an API key, "
        "token, password or private key?",
    ),
    PIICategory(
        label="GOV_ID",
        name="Government identifiers",
        includes="SSN, ITIN/EIN, passport numbers, driver's licence numbers, national IDs",
        hard_negatives="ZIP codes, area codes, order or invoice numbers with a similar shape",
        question="Does the text contain a government identifier such as a social security, "
        "passport, driver's licence or national ID number?",
    ),
    PIICategory(
        label="FINANCIAL",
        name="Financial accounts",
        includes="card numbers, bank account and routing numbers, IBAN/SWIFT tied to an account",
        hard_negatives="well-known test cards (4111 1111 1111 1111), amounts, invoice numbers",
        question="Does the text contain a payment card, bank account or routing number?",
    ),
    PIICategory(
        label="HEALTH",
        name="Health information",
        includes=(
            "medical record numbers, insurance member or group IDs, diagnoses or treatments "
            "tied to an identifiable person"
        ),
        hard_negatives="general medical discussion with no identifiable subject",
        question="Does the text contain health information about an identifiable person, "
        "or a medical record or insurance ID?",
    ),
    PIICategory(
        label="CONTACT",
        name="Direct contact details",
        includes="email addresses, phone numbers in any country format, postal addresses",
        hard_negatives=(
            "example.com, .test and .invalid addresses; documented fictional numbers "
            "(555-01xx); business switchboards in public documentation"
        ),
        question="Does the text contain a real person's email address, phone number or "
        "postal address?",
    ),
    PIICategory(
        label="PERSON_NAME",
        name="Names in context",
        includes="a real person's name together with other facts about them",
        hard_negatives=(
            "public figures in a public context, fictional characters, product and company "
            "names, a user signing their own message"
        ),
        question="Does the text name a private individual alongside facts about them?",
    ),
    PIICategory(
        label="QUASI_ID",
        name="Quasi-identifiers",
        includes=(
            "date of birth, precise location, employer plus role, ZIP code combined with age or sex"
        ),
        hard_negatives="dates and places not tied to a person",
        question="Does the text contain details that could identify a person in combination, "
        "such as a date of birth, precise location, or employer and role?",
    ),
    PIICategory(
        label="SPECIAL",
        name="Special-category data",
        includes=(
            "ethnicity, religion, sexual orientation, political opinion, union membership, "
            "criminal record or biometrics of an identifiable person"
        ),
        hard_negatives="abstract or statistical discussion of these topics",
        question="Does the text reveal ethnicity, religion, sexual orientation, political "
        "views, union membership, criminal record or biometrics of an identifiable person?",
    ),
    PIICategory(
        label="NETWORK",
        name="Network and device identifiers",
        includes="public IP addresses, MAC addresses, device or advertising IDs",
        hard_negatives=(
            "private and internal ranges (10.x, 172.16-31.x, 192.168.x, loopback), "
            "version numbers that look like IP addresses"
        ),
        question="Does the text contain a public IP address, MAC address or device ID?",
    ),
)

LABELS: tuple[str, ...] = tuple(c.label for c in CATEGORIES)
BY_LABEL: dict[str, PIICategory] = {c.label: c for c in CATEGORIES}

# JSON schema for the extractor's output (Ollama `format`): grammar-constrained,
# so the category is always one of LABELS and the shape always parses
FINDER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": list(LABELS)},
                    "value": {"type": "string"},
                },
                "required": ["category", "value"],
            },
        }
    },
    "required": ["findings"],
}


def finder_system_prompt() -> str:
    """Instructions for the extractor model, generated from the taxonomy."""
    lines = [
        "You find sensitive personal data and secrets in text.",
        "Return every value that belongs to one of these categories, copied EXACTLY as it",
        "appears in the text (same characters, same spacing). Never invent, normalize or",
        "complete a value. If nothing qualifies, return an empty list.",
        "",
        "Most texts contain none of these: an empty list is the normal answer. Only report",
        "information about a real, identifiable person, or a real secret. These are never",
        "findings: log lines, error messages, stack traces, service, host, process and",
        "code identifiers, public website URLs, timestamps, counters, version numbers,",
        'field labels with no value ("Email:"), and placeholders such as <IPV4>, <N>,',
        "[EMAIL] or {name}.",
        "",
        "A value is only the sensitive characters themselves, never a label or words around it.",
        'Example text: "Refund card 5500 0000 0000 0004 and email ana.diaz@mailbox.org; the',
        'server at 10.0.0.5 logged it." Answer: {"findings": [{"category": "FINANCIAL",',
        '"value": "5500 0000 0000 0004"}, {"category": "CONTACT", "value": "ana.diaz@mailbox.org"}]}',
        "(10.0.0.5 is an internal address, so it is not reported.)",
        "",
        "Categories:",
    ]
    for c in CATEGORIES:
        lines.append(f"- {c.label} ({c.name}): {c.includes}.")
        lines.append(f"  Do NOT report: {c.hard_negatives}.")
    lines += [
        "",
        'Respond only with JSON: {"findings": [{"category": "<LABEL>", "value": "<exact text>"}]}',
    ]
    return "\n".join(lines)
