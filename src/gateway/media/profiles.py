"""Engine profiles: an engine family's quirks as data (D-020).

Loaded from YAML files (config/profiles/<name>.yaml) at startup and
validated, so a broken profile fails loudly instead of silently producing
wrong dashboard controls or wrong validation.
"""

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from gateway.observability import get_logger

logger = get_logger(__name__)


class ParamSpec(BaseModel):
    """One per-request setting: its type, allowed values and default."""

    type: Literal["number", "integer", "boolean", "enum", "string"]
    min: float | None = None
    max: float | None = None
    values: list[Any] | None = None
    default: Any = None

    @model_validator(mode="after")
    def _enum_has_values(self) -> "ParamSpec":
        if self.type == "enum" and not self.values:
            raise ValueError("enum params need `values`")
        return self

    def check(self, name: str, value: Any) -> str | None:
        """Why value is invalid for this param, or None if it's fine."""
        if value is None:
            return None
        if self.type in ("number", "integer"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{name} must be a number"
            if self.type == "integer" and not float(value).is_integer():
                return f"{name} must be a whole number"
            if self.min is not None and value < self.min:
                return f"{name} must be at least {self.min:g}"
            if self.max is not None and value > self.max:
                return f"{name} must be at most {self.max:g}"
        elif self.type == "boolean":
            if not isinstance(value, bool):
                return f"{name} must be true or false"
        elif self.type == "enum":
            if value not in self.values:
                return f"{name} must be one of {self.values}"
        return None


class VoiceIdRule(BaseModel):
    """Derive voice attributes (language, gender) from the voice ID."""

    pattern: str
    language: dict[str, str] = Field(default_factory=dict)
    gender: dict[str, str] = Field(default_factory=dict)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, pattern: str) -> str:
        re.compile(pattern)
        return pattern

    def attributes(self, voice_id: str) -> dict[str, str]:
        match = re.match(self.pattern, voice_id)
        if not match:
            return {}
        groups = match.groupdict()
        attrs = {}
        if groups.get("language") is not None:
            attrs["language"] = self.language.get(groups["language"], groups["language"])
        if groups.get("gender") is not None:
            attrs["gender"] = self.gender.get(groups["gender"], groups["gender"])
        return attrs


class TTSProfile(BaseModel):
    voices_path: str | None = "/v1/audio/voices"
    voice_id: VoiceIdRule | None = None
    blending: bool = False
    params: dict[str, ParamSpec] = Field(default_factory=dict)


class STTProfile(BaseModel):
    languages: list[str] = Field(default_factory=list)
    params: dict[str, ParamSpec] = Field(default_factory=dict)


class EngineProfile(BaseModel):
    name: str
    description: str = ""
    tts: TTSProfile | None = None
    stt: STTProfile | None = None


def load_profiles(directory: str | Path) -> dict[str, EngineProfile]:
    """Load every <name>.yaml in directory. Raises on an invalid profile."""
    path = Path(directory)
    if not path.is_dir():
        return {}
    profiles: dict[str, EngineProfile] = {}
    for file in sorted(path.glob("*.yaml")):
        data = yaml.safe_load(file.read_text()) or {}
        try:
            profiles[file.stem] = EngineProfile(name=file.stem, **data)
        except Exception as e:
            raise ValueError(f"Invalid engine profile {file}: {e}") from e
    logger.info("Engine profiles loaded", profiles=sorted(profiles))
    return profiles


# Voice mixes: "af_bella(2)+af_sky(1)", "a+b-c" (Kokoro-FastAPI syntax)
_MIX_SPLIT = re.compile(r"[+-]")
_MIX_WEIGHT = re.compile(r"\(\s*[0-9.]+\s*\)\s*$")


def voice_components(voice: str, blending: bool) -> list[str]:
    """The individual voice IDs in a voice string (one, unless blending)."""
    if not blending or not _MIX_SPLIT.search(voice):
        return [voice]
    return [_MIX_WEIGHT.sub("", part).strip() for part in _MIX_SPLIT.split(voice) if part.strip()]
