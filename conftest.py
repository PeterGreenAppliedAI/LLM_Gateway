"""Pytest configuration and fixtures."""

import pytest


@pytest.fixture(autouse=True)
def reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset environment variables before each test."""
    # Clear any GATEWAY_ prefixed env vars that might interfere
    import os

    for key in list(os.environ.keys()):
        if key.startswith("GATEWAY_"):
            monkeypatch.delenv(key, raising=False)
