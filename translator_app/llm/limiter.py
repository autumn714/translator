"""Priority-aware concurrency limiter for upstream LLM requests.

The vLLM server is shared with another application (8 scheduler slots, FCFS), so this app
caps its own in-flight requests:

* at most ``capacity`` requests in total (LLM_MAX_PARALLEL),
* at most ``doc_capacity`` of them for document jobs (LLM_DOC_PARALLEL),
* waiting interactive requests are always granted before waiting document requests,
* while the server reports queued requests (``congested``), capacity drops to 1 and
  document requests wait entirely.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

Kind = Literal["interactive", "document"]
KINDS: tuple[Kind, Kind] = ("interactive", "document")


class PriorityLimiter:
    def __init__(self, capacity: int, doc_capacity: int, *, on_demand: Callable[[], None] | None = None) -> None:
        self._max_capacity = max(1, int(capacity))
        self._max_doc = max(1, min(int(doc_capacity), self._max_capacity))
        self._capacity = self._max_capacity
        self._congested = False
        self._active: dict[str, int] = {kind: 0 for kind in KINDS}
        self._waiters: dict[str, deque[asyncio.Future[None]]] = {kind: deque() for kind in KINDS}
        self._on_demand = on_demand

    # ------------------------------------------------------------------ state
    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def max_capacity(self) -> int:
        return self._max_capacity

    @property
    def congested(self) -> bool:
        return self._congested

    @property
    def doc_capacity(self) -> int:
        return 0 if self._congested else min(self._max_doc, self._capacity)

    @property
    def active(self) -> int:
        return self._active["interactive"] + self._active["document"]

    def waiting(self, kind: Kind | None = None) -> int:
        if kind is None:
            return sum(self._count_waiters(k) for k in KINDS)
        return self._count_waiters(kind)

    @property
    def idle(self) -> bool:
        return self.active == 0 and self.waiting() == 0

    def snapshot(self) -> dict[str, int | bool]:
        return {
            "capacity": self._capacity,
            "congested": self._congested,
            "active_interactive": self._active["interactive"],
            "active_document": self._active["document"],
            "waiting_interactive": self.waiting("interactive"),
            "waiting_document": self.waiting("document"),
        }

    def set_congested(self, congested: bool) -> None:
        """Server has queued requests -> keep a single slot (interactive only)."""
        self._congested = bool(congested)
        self._capacity = 1 if self._congested else self._max_capacity
        self._wake()

    # ------------------------------------------------------------------ acquire / release
    @asynccontextmanager
    async def slot(self, kind: Kind = "interactive") -> AsyncIterator[None]:
        await self.acquire(kind)
        try:
            yield
        finally:
            self.release(kind)

    async def acquire(self, kind: Kind = "interactive") -> None:
        kind = _check_kind(kind)
        if self._on_demand is not None:
            self._on_demand()
        if not self._has_waiters_ahead(kind) and self._can_grant(kind):
            self._active[kind] += 1
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters[kind].append(future)
        try:
            await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                # Granted at the same moment the waiter was cancelled: give the slot back.
                self.release(kind)
            else:
                try:
                    self._waiters[kind].remove(future)
                except ValueError:
                    pass
                self._wake()
            raise

    def release(self, kind: Kind = "interactive") -> None:
        kind = _check_kind(kind)
        if self._active[kind] > 0:
            self._active[kind] -= 1
        self._wake()

    # ------------------------------------------------------------------ internals
    def _count_waiters(self, kind: str) -> int:
        return sum(1 for fut in self._waiters[kind] if not fut.done())

    def _has_waiters_ahead(self, kind: str) -> bool:
        if self._count_waiters(kind):
            return True
        return kind == "document" and self._count_waiters("interactive") > 0

    def _can_grant(self, kind: str) -> bool:
        if self.active >= self._capacity:
            return False
        if kind == "document":
            if self._count_waiters("interactive"):
                return False
            if self._active["document"] >= self.doc_capacity:
                return False
        return True

    def _wake(self) -> None:
        for kind in KINDS:
            queue = self._waiters[kind]
            while queue:
                head = queue[0]
                if head.done():
                    queue.popleft()
                    continue
                if not self._can_grant(kind):
                    break
                queue.popleft()
                self._active[kind] += 1
                head.set_result(None)


def _check_kind(kind: str) -> Kind:
    if kind not in KINDS:
        raise ValueError(f"unknown priority: {kind}")
    return kind  # type: ignore[return-value]
