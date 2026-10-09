"""Per-category ML PII policy, editable from the dashboard (D-052).

Each taxonomy category is either:

- ``detect``: the shadow pipeline records it (counts, categories, timings).
  The default for every category: nothing is enforced until an operator
  chooses to, with the gate's measured miss rate in front of them.
- ``scrub_stored``: additionally, values the extractor finds in that
  category are replaced with ``[LABEL]`` in the request's stored copies
  (audit request/response bodies, stored security-scan messages).

What the model receives is never changed by this policy; the regex scrub
switch (``pii.scrub``) still governs that. Enforcement is only as good as
detection: a value the gate misses and the sample doesn't catch is not
scrubbed, which is why the dashboard shows the miss rate beside the switches.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from gateway.security.pii_taxonomy import LABELS

SETTING_KEY = "pii.ml_policy"

Action = Literal["detect", "scrub_stored"]
ACTIONS: tuple[str, ...] = ("detect", "scrub_stored")


class PIIMLPolicyUpdate(BaseModel):
    """A policy change submitted from the dashboard: only the categories sent change."""

    categories: dict[str, Action] = Field(default_factory=dict)

    @field_validator("categories")
    @classmethod
    def _known_labels(cls, categories: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(categories) - set(LABELS))
        if unknown:
            raise ValueError(f"Unknown categories {unknown}; known: {list(LABELS)}")
        return categories


class PIIMLPolicy(BaseModel):
    """Policy in effect."""

    categories: dict[str, Action] = Field(default_factory=lambda: dict.fromkeys(LABELS, "detect"))
    source: Literal["default", "dashboard"] = "default"
    updated_at: datetime | None = None
    updated_by: str | None = None

    def scrub_labels(self) -> set[str]:
        return {label for label, action in self.categories.items() if action == "scrub_stored"}

    def merged(self, update: PIIMLPolicyUpdate) -> dict[str, str]:
        return {**self.categories, **update.categories}

    @classmethod
    def from_saved(cls, saved: dict) -> "PIIMLPolicy":
        update = PIIMLPolicyUpdate.model_validate(saved["value"])
        return cls(
            categories={**dict.fromkeys(LABELS, "detect"), **update.categories},
            source="dashboard",
            updated_at=saved.get("updated_at"),
            updated_by=saved.get("updated_by"),
        )
