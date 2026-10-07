"""Media catalog: what each voice endpoint offers, discovered and normalized.

Engines describe themselves inconsistently (D-020). The catalog asks each
media endpoint for its voices and models every `interval` seconds,
normalizes the answers, adds what its profile says (languages, genders,
setting ranges), and falls back to admin-declared voices. Two consumers:

- routing and validation: `check_speech()` says why an endpoint can't serve
  a request (unknown voice, speed out of range), so requests route only to
  endpoints that can, and fail with 422 when none can
- the dashboard and `GET /v1/audio/voices`: `snapshot()` / `voices()`

An endpoint that lists nothing is never blocked: unknown is not invalid.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

from gateway.dispatch.registry import ProviderRegistry
from gateway.media.profiles import EngineProfile, voice_components
from gateway.observability import get_logger

logger = get_logger(__name__)

# Voice IDs listed in a 422 before "(+N more)"
_MAX_LISTED = 20


def normalize_voices(data: Any) -> list[dict]:
    """Voice lists in every shape seen in the wild, as [{id, name, ...}].

    Shapes: {"voices": [obj|str], ...} (Kokoro-FastAPI, speaches,
    vLLM-Omni, Chatterbox), {"voices": [...], "uploaded_voices": [obj]}
    (vLLM-Omni), a bare list, or {id: config} (Piper).
    """
    items: list = []
    if isinstance(data, dict) and "voices" in data:
        items = list(data.get("voices") or [])
        items += list(data.get("uploaded_voices") or [])
    elif isinstance(data, dict):
        items = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in data.items()]
    elif isinstance(data, list):
        items = data

    voices: dict[str, dict] = {}
    for item in items:
        if isinstance(item, str):
            voices.setdefault(item, {"id": item, "name": item})
            continue
        if not isinstance(item, dict):
            continue
        voice_id = item.get("id") or item.get("voice_id") or item.get("name")
        if not voice_id:
            continue
        voice = {"id": str(voice_id), "name": str(item.get("name") or voice_id)}
        language = item.get("language")
        if isinstance(language, dict):  # Piper: {"code": "en_US", "name_english": ...}
            language = language.get("name_english") or language.get("code")
        if language:
            voice["language"] = str(language)
        if item.get("gender"):
            voice["gender"] = str(item["gender"])
        voices.setdefault(voice["id"], voice)
    return list(voices.values())


class MediaCatalog:
    def __init__(
        self,
        registry: ProviderRegistry,
        profiles: dict[str, EngineProfile],
        interval: float = 60.0,
    ):
        self._registry = registry
        self._profiles = profiles
        self._interval = interval
        self._entries: dict[str, dict] = {}
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lookup

    def _media_endpoints(self) -> list[str]:
        return [
            name
            for name in self._registry.list_providers()
            if getattr(self._registry.get_endpoint_config(name), "capabilities", None)
        ]

    def profile_for(self, endpoint: str) -> EngineProfile | None:
        config = self._registry.get_endpoint_config(endpoint)
        name = getattr(config, "profile", None)
        return self._profiles.get(name) if name else None

    def voices_for(self, endpoint: str) -> list[dict]:
        return self._entries.get(endpoint, {}).get("voices", [])

    # -------------------------------------------------------------- discovery

    async def refresh(self) -> None:
        await asyncio.gather(
            *(self._refresh_endpoint(name) for name in self._media_endpoints()),
            return_exceptions=True,
        )

    async def _refresh_endpoint(self, name: str) -> None:
        config = self._registry.get_endpoint_config(name)
        adapter = self._registry.get(name)
        profile = self.profile_for(name)
        entry: dict[str, Any] = {
            "models": [],
            "voices": [],
            "voices_source": None,
            "error": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        errors = []
        try:
            client = await adapter.media_client()
        except Exception as e:  # adapter can't serve media (shouldn't happen: config-validated)
            entry["error"] = str(e)
            self._entries[name] = entry
            return

        try:
            response = await client.get("/v1/models", timeout=10.0)
            if response.status_code < 400:
                data = response.json().get("data", [])
                entry["models"] = [
                    {"id": m["id"], **({"task": m["task"]} if m.get("task") else {})}
                    for m in data
                    if isinstance(m, dict) and m.get("id")
                ]
        except Exception as e:
            errors.append(f"models: {e}")

        voices_path = profile.tts.voices_path if profile and profile.tts else "/v1/audio/voices"
        if "tts" in config.capabilities and voices_path:
            try:
                response = await client.get(voices_path, timeout=10.0)
                if response.status_code < 400:
                    entry["voices"] = normalize_voices(response.json())
                    entry["voices_source"] = "engine" if entry["voices"] else None
            except Exception as e:
                errors.append(f"voices: {e}")

        if not entry["voices"] and config.voices:
            entry["voices"] = [{"id": v, "name": v} for v in config.voices]
            entry["voices_source"] = "config"

        # Attributes the profile can derive from IDs (Kokoro: af_ -> American English, female)
        rule = profile.tts.voice_id if profile and profile.tts else None
        if rule:
            for voice in entry["voices"]:
                for key, value in rule.attributes(voice["id"]).items():
                    voice.setdefault(key, value)

        entry["error"] = "; ".join(errors) or None
        self._entries[name] = entry

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Media catalog refresh failed")
            await asyncio.sleep(self._interval)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------- validation

    def check_speech(self, endpoint: str, voice: Any, params: dict[str, Any]) -> str | None:
        """Why endpoint can't serve this speech request, or None if it can
        (or if nothing is known about it)."""
        profile = self.profile_for(endpoint)
        tts = profile.tts if profile else None

        for name, spec in (tts.params if tts else {}).items():
            reason = spec.check(name, params.get(name))
            if reason:
                return reason

        known = {v["id"] for v in self.voices_for(endpoint)}
        if not known:
            return None
        voice_id = voice.get("id") if isinstance(voice, dict) else voice
        if not isinstance(voice_id, str):
            return None
        for component in voice_components(voice_id, bool(tts and tts.blending)):
            if component not in known:
                listed = sorted(known)
                more = len(listed) - _MAX_LISTED
                suffix = f" (+{more} more)" if more > 0 else ""
                return (
                    f"Voice '{component}' is not available; available voices: "
                    f"{', '.join(listed[:_MAX_LISTED])}{suffix}"
                )
        return None

    # ------------------------------------------------------------------ views

    def voices(self, endpoints: list[str]) -> list[dict]:
        """Union of voices on the given endpoints, each listing where it's served."""
        merged: dict[str, dict] = {}
        for endpoint in endpoints:
            for voice in self.voices_for(endpoint):
                entry = merged.setdefault(voice["id"], {**voice, "endpoints": []})
                entry["endpoints"].append(endpoint)
        return sorted(merged.values(), key=lambda v: v["id"])

    def snapshot(self) -> list[dict]:
        """Everything the dashboard needs per media endpoint."""
        result = []
        for name in self._media_endpoints():
            config = self._registry.get_endpoint_config(name)
            profile = self.profile_for(name)
            entry = self._entries.get(name, {})
            result.append(
                {
                    "endpoint": name,
                    "capabilities": list(config.capabilities),
                    "healthy": self._registry.is_healthy(name),
                    "profile": profile.name if profile else None,
                    "description": profile.description if profile else "",
                    "models": entry.get("models", []),
                    "voices": entry.get("voices", []),
                    "voices_source": entry.get("voices_source"),
                    "blending": bool(profile and profile.tts and profile.tts.blending),
                    "tts_params": {
                        k: v.model_dump(exclude_none=True)
                        for k, v in (profile.tts.params if profile and profile.tts else {}).items()
                    },
                    "stt_params": {
                        k: v.model_dump(exclude_none=True)
                        for k, v in (profile.stt.params if profile and profile.stt else {}).items()
                    },
                    "stt_languages": profile.stt.languages if profile and profile.stt else [],
                    "fetched_at": entry.get("fetched_at"),
                    "error": entry.get("error"),
                }
            )
        return result
