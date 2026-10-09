"""Pytest configuration and fixtures."""

import pytest

# Tests must not read the operator's local .env (admin keys, access modes,
# retention overrides): pydantic-settings loads the FILE directly, so the
# env-var scrub below doesn't cover it. CI has no .env; mirror that.
from gateway.settings import Settings

Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset environment variables before each test."""
    # Clear any GATEWAY_ prefixed env vars that might interfere
    import os

    for key in list(os.environ.keys()):
        if key.startswith("GATEWAY_"):
            monkeypatch.delenv(key, raising=False)
