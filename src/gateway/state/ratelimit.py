"""Sliding-window request counts for the rate limiter (D-035).

The limiter (policy/rate_limiter.py) decides the limits; a store only
counts. `hit` checks every window and records the request only if all
of them have room, atomically, so two processes can't both take the last
slot of a shared window.
"""

import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from gateway.state.concurrency import StoreHealth, is_backend_error

# Maximum unique keys tracked in memory (prevents memory exhaustion)
MAX_TRACKED_KEYS = 10000


@dataclass
class WindowResult:
    allowed: bool
    counts: list[int]  # requests already in each window, before this one
    exceeded: int | None = None  # index of the first full window
    retry_after: float = 0.0  # seconds until that window has room


class RateWindowStore(Protocol):
    async def hit(self, key: str, windows: Sequence[tuple[float, int]]) -> WindowResult:
        """Record a request if every (window_seconds, limit) has room."""

    async def counts(self, key: str, windows: Sequence[float]) -> list[int]: ...

    async def reset(self, key: str) -> None: ...

    async def reset_all(self) -> None: ...


class InMemoryRateStore:
    """Per-process timestamps (the default)."""

    def __init__(self) -> None:
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _trim(self, key: str, now: float, horizon: float) -> list[float]:
        if len(self._requests) >= MAX_TRACKED_KEYS and key not in self._requests:
            stale = [k for k, ts in self._requests.items() if not ts or now - ts[-1] > horizon]
            for k in stale[: len(stale) // 2 + 1]:
                del self._requests[k]
        kept = [ts for ts in self._requests[key] if now - ts < horizon]
        self._requests[key] = kept
        return kept

    async def hit(self, key: str, windows: Sequence[tuple[float, int]]) -> WindowResult:
        now = time.time()
        requests = self._trim(key, now, max(w for w, _ in windows))
        counts = []
        for i, (window, limit) in enumerate(windows):
            inside = [ts for ts in requests if now - ts < window]
            counts.append(len(inside))
            if len(inside) >= limit:
                return WindowResult(False, counts, i, max(0.1, window - (now - min(inside))))
        requests.append(now)
        return WindowResult(True, counts)

    async def counts(self, key: str, windows: Sequence[float]) -> list[int]:
        now = time.time()
        requests = self._trim(key, now, max(windows))
        return [sum(1 for ts in requests if now - ts < w) for w in windows]

    async def reset(self, key: str) -> None:
        self._requests.pop(key, None)

    async def reset_all(self) -> None:
        self._requests.clear()


# One sorted set per key: members are unique request ids, scores the Redis
# server's clock in ms. ARGV: id, then window_ms/limit pairs.
_HIT = """
local t = redis.call('TIME'); local now = t[1] * 1000 + math.floor(t[2] / 1000)
local horizon = 0
for i = 2, #ARGV, 2 do horizon = math.max(horizon, tonumber(ARGV[i])) end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - horizon)
local counts = {}
for i = 2, #ARGV, 2 do
  local window = tonumber(ARGV[i]); local limit = tonumber(ARGV[i + 1])
  local n = redis.call('ZCOUNT', KEYS[1], '(' .. (now - window), '+inf')
  table.insert(counts, n)
  if n >= limit then
    local oldest = redis.call('ZRANGEBYSCORE', KEYS[1], '(' .. (now - window), '+inf', 'WITHSCORES', 'LIMIT', 0, 1)
    return {0, (i / 2) - 1, window - (now - tonumber(oldest[2])), counts}
  end
end
redis.call('ZADD', KEYS[1], now, ARGV[1])
redis.call('PEXPIRE', KEYS[1], horizon)
return {1, -1, 0, counts}
"""

_COUNTS = """
local t = redis.call('TIME'); local now = t[1] * 1000 + math.floor(t[2] / 1000)
local counts = {}
for i = 1, #ARGV do
  table.insert(counts, redis.call('ZCOUNT', KEYS[1], '(' .. (now - tonumber(ARGV[i])), '+inf'))
end
return counts
"""


class RedisRateStore:
    """Counts shared by every gateway process using the same Redis."""

    def __init__(self, client: Any, prefix: str):
        self._client = client
        self._base = f"{prefix}:rl"
        self._hit = client.register_script(_HIT)
        self._counts = client.register_script(_COUNTS)

    def _key(self, key: str) -> str:
        return f"{self._base}:{key}"

    async def hit(self, key: str, windows: Sequence[tuple[float, int]]) -> WindowResult:
        args: list = [uuid.uuid4().hex]
        for window, limit in windows:
            args += [int(window * 1000), limit]
        allowed, exceeded, retry_ms, counts = await self._hit(keys=[self._key(key)], args=args)
        counts = [int(c) for c in counts]
        if allowed:
            return WindowResult(True, counts)
        return WindowResult(False, counts, int(exceeded), max(0.1, int(retry_ms) / 1000))

    async def counts(self, key: str, windows: Sequence[float]) -> list[int]:
        result = await self._counts(keys=[self._key(key)], args=[int(w * 1000) for w in windows])
        return [int(c) for c in result]

    async def reset(self, key: str) -> None:
        await self._client.delete(self._key(key))

    async def reset_all(self) -> None:
        async for name in self._client.scan_iter(match=f"{self._base}:*", count=500):
            await self._client.delete(name)


class FallbackRateStore:
    """Redis when reachable, else this process's own counts (as without Redis)."""

    def __init__(self, primary: RedisRateStore, fallback: InMemoryRateStore, health: StoreHealth):
        self._primary = primary
        self._fallback = fallback
        self.health = health

    async def _call(self, method: str, *args):
        if self.health.use_primary():
            try:
                result = await getattr(self._primary, method)(*args)
            except Exception as e:
                if not is_backend_error(e):
                    raise
                self.health.failed(e)
            else:
                self.health.ok()
                return result
        return await getattr(self._fallback, method)(*args)

    async def hit(self, key: str, windows: Sequence[tuple[float, int]]) -> WindowResult:
        return await self._call("hit", key, windows)

    async def counts(self, key: str, windows: Sequence[float]) -> list[int]:
        return await self._call("counts", key, windows)

    async def reset(self, key: str) -> None:
        await self._fallback.reset(key)
        await self._call("reset", key)

    async def reset_all(self) -> None:
        await self._fallback.reset_all()
        await self._call("reset_all")
