"""Cache of validated database-backed API keys (D-040).

Without it, every request with a DB-backed key ran a SELECT plus an
UPDATE of last_used_at and a commit, on SQLite's single writer, before the
request could start. Now:

- A validated key is remembered for `ttl` seconds (default 30): repeat
  requests do no database work at all. An expiry date on the key is still
  checked on every hit.
- An unknown key is remembered as unknown for a few seconds, so a client
  spraying random keys can't turn each guess into a database query.
- Revoking a key drops it from this process's cache at once; other gateway
  processes stop accepting it within `ttl`.
- last_used_at is an informational timestamp (every request is already in
  the audit log), so uses are coalesced in memory and written in one
  transaction every `flush_interval` seconds, and on shutdown. A crash
  loses at most that much last_used_at freshness, never an audit row.
"""

import asyncio
import contextlib
import time
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.observability import get_logger
from gateway.storage.keys import KeyManager, _hash_key

logger = get_logger(__name__)


class KeyCache:
    def __init__(
        self,
        engine: AsyncEngine,
        ttl: float = 30.0,
        negative_ttl: float = 5.0,
        max_entries: int = 10_000,
        flush_interval: float = 30.0,
    ):
        self._keys = KeyManager(engine)
        self._ttl = ttl
        self._negative_ttl = negative_ttl
        self._max = max_entries
        self._flush_interval = flush_interval
        self._valid: OrderedDict[str, tuple[dict, float]] = OrderedDict()
        self._invalid: OrderedDict[str, float] = OrderedDict()
        self._last_used: dict[int, datetime] = {}
        self._task: asyncio.Task | None = None
        self.hits = 0
        self.misses = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._flush_loop(), name="key-last-used")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()

    async def validate(self, plaintext: str) -> dict | None:
        """The key's metadata if it's an active DB-backed key, else None."""
        key_hash = _hash_key(plaintext)
        now = time.monotonic()

        cached = self._valid.get(key_hash)
        if cached is not None and now - cached[1] < self._ttl:
            info = cached[0]
            expires_at = info.get("expires_at")
            if expires_at is None or datetime.now(timezone.utc) < expires_at:
                self.hits += 1
                self._valid.move_to_end(key_hash)
                self._record_use(info["id"])
                return info
            self._valid.pop(key_hash, None)  # expired since it was cached

        denied_at = self._invalid.get(key_hash)
        if denied_at is not None and now - denied_at < self._negative_ttl:
            self.hits += 1
            return None

        self.misses += 1
        info = await self._keys.lookup_by_hash(key_hash)
        if info is None:
            self._remember(self._invalid, key_hash, now)
            self._valid.pop(key_hash, None)
            return None
        self._invalid.pop(key_hash, None)
        self._remember(self._valid, key_hash, (info, now))
        self._record_use(info["id"])
        return info

    def _remember(self, store: OrderedDict, key_hash: str, value) -> None:
        store[key_hash] = value
        store.move_to_end(key_hash)
        while len(store) > self._max:
            store.popitem(last=False)

    def _record_use(self, key_id: int) -> None:
        self._last_used[key_id] = datetime.now(timezone.utc)

    def invalidate_key(self, key_id: int) -> None:
        """A key was revoked: stop accepting it here immediately."""
        for key_hash, (info, _) in list(self._valid.items()):
            if info["id"] == key_id:
                del self._valid[key_hash]

    def forget_plaintext(self, plaintext: str) -> None:
        """A key was just created: drop any 'unknown key' entry for it."""
        self._invalid.pop(_hash_key(plaintext), None)

    async def flush(self) -> None:
        pending, self._last_used = self._last_used, {}
        if not pending:
            return
        try:
            await self._keys.touch(pending)
        except Exception as e:
            # Informational only: keep the newest times for the next attempt
            for key_id, used_at in pending.items():
                if self._last_used.get(key_id, used_at) <= used_at:
                    self._last_used[key_id] = used_at
            logger.warning("Couldn't update API key last_used_at; will retry", error=str(e))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush()
