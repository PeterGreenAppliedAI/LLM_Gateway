"""Who may use the gateway without a key, and from where (D-042).

Access modes:
- keys (auth.enabled: true): every request needs a key. Keyless requests
  only if auth.anonymous.enabled, and only from auth.anonymous.allowed_networks.
  Admin routes need GATEWAY_ADMIN_API_KEY.
- solo (auth.enabled: false): no keys, for one person on one machine.
  Requests only from auth.anonymous.allowed_networks (default: this machine).
- dev (GATEWAY_DEV_MODE=true): test mode. Keyless inference and dashboard
  from GATEWAY_DEV_NETWORKS (default: this machine), whatever the auth
  config says, so a real config can be tried without keys. Requests that do
  send a key are still checked. Loud everywhere: startup banner, /health,
  dashboard banner, audit rows under client "dev".

"From this machine" is decided on the TCP peer address. A request carrying
proxy headers never counts as local: with a reverse proxy on the same host,
every internet request would otherwise arrive from 127.0.0.1. Trusted-proxy
support is a separate step.
"""

import ipaddress
from collections.abc import Iterable

from fastapi import Request

LOCAL_NETWORKS = ("127.0.0.0/8", "::1/128")
DEV_CLIENT_ID = "dev"
_PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host")


def parse_networks(values: Iterable[str]) -> list[ipaddress._BaseNetwork]:
    """Validate CIDR strings ("10.0.0.0/8", "192.168.1.20"); raise ValueError on a bad one."""
    return [ipaddress.ip_network(v.strip(), strict=False) for v in values if v.strip()]


def from_networks(request: Request, networks: Iterable[str]) -> bool:
    """Whether the request comes directly from one of these networks."""
    if any(h in request.headers for h in _PROXY_HEADERS):
        return False
    host = request.client.host if request.client else None
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Test clients and unix sockets report names, not addresses
        return host in ("testclient", "localhost")
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return any(address in network for network in parse_networks(networks))


def dev_mode_allows(request: Request) -> bool:
    """Test mode is on and this request comes from a test network."""
    from gateway.settings import get_settings

    settings = get_settings()
    return settings.dev_mode and from_networks(request, settings.dev_networks)


def source(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def access_status(config, settings) -> dict:
    """For /health and the dashboard: which mode, and who may skip keys."""
    if settings.dev_mode:
        return {"mode": "dev", "keyless_from": list(settings.dev_networks)}
    if not config.auth.enabled:
        return {"mode": "solo", "keyless_from": list(config.auth.anonymous.allowed_networks)}
    anonymous = config.auth.anonymous
    return {
        "mode": "keys",
        "keyless_from": list(anonymous.allowed_networks) if anonymous.enabled else [],
        "admin_key_configured": settings.admin_api_key is not None,
    }


def production_problems(config, settings) -> list[str]:
    """What GATEWAY_PROFILE=production refuses to start with."""
    problems = []
    if settings.dev_mode:
        problems.append("GATEWAY_DEV_MODE is on (keyless test access)")
    if not config.auth.enabled:
        problems.append("auth.enabled is false (no API keys)")
    if settings.admin_api_key is None:
        problems.append("GATEWAY_ADMIN_API_KEY is not set (no operator credential)")
    anonymous = config.auth.anonymous
    if config.auth.enabled and anonymous.enabled and anonymous.unrestricted:
        problems.append("auth.anonymous is enabled without model, endpoint or rate restrictions")
    return problems
