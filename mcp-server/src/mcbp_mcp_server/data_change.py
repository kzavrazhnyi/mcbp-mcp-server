"""The base's own "AI data change allowed" switch, as reported by `GET /ai/v1/health`.

BAS enforces it by itself (every write route answers 403 `DATA_CHANGE_DISABLED` while it is off),
so this gate only spares the model write tools it can never use: `tools/list` hides them and a
`tools/call` to one is refused without a round-trip to 1C. `MCP_ALLOW_WRITE` still applies on top —
both must allow.

The value is seeded by the startup health probe and refreshed at most once per `TTL_S`, on the
next `tools/list` or write `tools/call` that finds it stale — never one health call per tool call.
`None` (older base without the field, or health unreachable so far) means "not enforced here".
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger("mcbp_mcp_server")

TTL_S = 60.0


class _HealthClient(Protocol):
    async def health(self) -> dict[str, Any]: ...


class DataChangeGate:
    def __init__(self, client: _HealthClient, *, ttl_s: float = TTL_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._clock = clock
        self._value: bool | None = None
        self._stamp: float | None = None

    def update(self, health: dict[str, Any] | None) -> None:
        """Takes the flag from an already-fetched health payload; `None` (fetch failed) keeps the
        last known value but still restarts the TTL, so a dead base is not polled per call."""
        if health is not None:
            value = health.get("allow_data_change")
            self._value = value if isinstance(value, bool) else None
        self._stamp = self._clock()

    def mark_disabled(self) -> None:
        """BAS itself answered `DATA_CHANGE_DISABLED` — trust it until the next refresh."""
        self._value = False
        self._stamp = self._clock()

    async def allowed(self) -> bool | None:
        """`False` only when the base says data change is off; `True`/`None` = do not block."""
        if self._stamp is None or self._clock() - self._stamp >= self._ttl_s:
            try:
                health = await self._client.health()
            except Exception as e:  # noqa: BLE001 - advisory: BAS still enforces the switch
                log.warning("MCBP_AI health refresh failed (%s); keeping allow_data_change=%s",
                            e, self._value)
                health = None
            self.update(health)
        return self._value
