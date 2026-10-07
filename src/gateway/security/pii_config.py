"""Runtime PII scrubbing configuration, editable from the dashboard.

Detection on/off stays an environment setting (GATEWAY_PII_ENABLED):
turning detection off also stops redaction of stored data (D-005), which
shouldn't be one dashboard click away. Scrubbing (whether the model gets
PII-replaced text, and on which routes) is operator policy that changes
at runtime, so it lives here: environment variables give the startup
default, a saved dashboard value overrides it and survives restarts.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Routes that run PII detection, and therefore can scrub
PII_SCAN_ROUTES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/api/chat",
    "/api/generate",
    "/api/embed",
    "/api/embeddings",
    "/v1/audio/speech",  # TTS input text (scrubbed text is spoken as "[EMAIL]" etc.)
)

SETTING_KEY = "pii.scrub"


class PIIScrubUpdate(BaseModel):
    """A scrubbing change submitted from the dashboard."""

    scrub_enabled: bool
    # Empty = every route in PII_SCAN_ROUTES
    scrub_routes: list[str] = Field(default_factory=list, max_length=len(PII_SCAN_ROUTES))

    @field_validator("scrub_routes")
    @classmethod
    def _known_routes(cls, routes: list[str]) -> list[str]:
        # A typo would otherwise silently leave the intended route unscrubbed
        unknown = [r for r in routes if r not in PII_SCAN_ROUTES]
        if unknown:
            raise ValueError(
                f"Unknown routes {unknown}; scrubbable routes are {list(PII_SCAN_ROUTES)}"
            )
        return list(dict.fromkeys(routes))


class PIIScrubConfig(BaseModel):
    """Scrubbing policy currently in effect (read by should_scrub_pii)."""

    scrub_enabled: bool = False
    scrub_routes: list[str] = Field(default_factory=list)
    source: Literal["environment", "dashboard"] = "environment"
    updated_at: datetime | None = None
    updated_by: str | None = None

    @classmethod
    def from_saved(cls, saved: dict) -> "PIIScrubConfig":
        """From a runtime_settings row; validates like a fresh update."""
        update = PIIScrubUpdate.model_validate(saved["value"])
        return cls(
            scrub_enabled=update.scrub_enabled,
            scrub_routes=update.scrub_routes,
            source="dashboard",
            updated_at=saved.get("updated_at"),
            updated_by=saved.get("updated_by"),
        )
