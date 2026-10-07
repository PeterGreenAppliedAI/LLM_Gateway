"""Upstream credentials for an endpoint (shared by adapters and discovery)."""

import os

# Well-known env vars, by a word in the endpoint name, for cloud providers
_DEFAULT_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
}


def resolve_api_key(name: str, api_key: str | None, api_key_env: str | None) -> str | None:
    """The key to send upstream, if any.

    Order: `api_key` (literal, or `${ENV_VAR}`), then `api_key_env`, then a
    well-known variable matched from the endpoint name (e.g. "openai-cloud"
    reads OPENAI_API_KEY).
    """
    if api_key:
        if api_key.startswith("${") and api_key.endswith("}"):
            return os.environ.get(api_key[2:-1])
        return api_key
    if api_key_env:
        return os.environ.get(api_key_env)
    lowered = name.lower()
    for word, env in _DEFAULT_KEY_ENV.items():
        if word in lowered:
            return os.environ.get(env)
    return None


def auth_headers(
    name: str, api_key: str | None, api_key_env: str | None, headers: dict[str, str] | None
) -> dict[str, str]:
    """Custom headers plus `Authorization: Bearer <key>` when a key resolves."""
    result = dict(headers or {})
    if key := resolve_api_key(name, api_key, api_key_env):
        result["Authorization"] = f"Bearer {key}"
    return result
