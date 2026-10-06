"""PriorityLimiter: total cap, document cap, interactive-first, congestion."""
from __future__ import annotations

import asyncio

import pytest

from translator_app.llm.limiter import PriorityLimiter

pytestmark = pytest.mark.anyio


async def _hold(limiter: PriorityLimiter, kind: str, log: list[str], name: str, release: asyncio.Event) -> None:
    async with limiter.slot(kind):  # type: ignore[arg-type]
        log.append(name)
        await release.wait()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_caps_total_and_document_slots() -> None:
    limiter = PriorityLimiter(3, 2)
    release = asyncio.Event()
    log: list[str] = []
    tasks = [asyncio.create_task(_hold(limiter, "document", log, f"d{i}", release)) for i in range(4)]
    await _settle()
    assert log == ["d0", "d1"]  # documents never take more than LLM_DOC_PARALLEL
    assert limiter.active == 2
    tasks.append(asyncio.create_task(_hold(limiter, "interactive", log, "i0", release)))
    tasks.append(asyncio.create_task(_hold(limiter, "interactive", log, "i1", release)))
    await _settle()
    assert log == ["d0", "d1", "i0"]  # total cap 3
    assert limiter.waiting("interactive") == 1 and limiter.waiting("document") == 2
    release.set()
    await asyncio.gather(*tasks)
    assert limiter.idle


async def test_waiting_interactive_requests_go_first() -> None:
    limiter = PriorityLimiter(1, 1)
    gates = [asyncio.Event() for _ in range(5)]
    log: list[str] = []
    first = asyncio.create_task(_hold(limiter, "document", log, "d0", gates[0]))
    await _settle()
    waiting = [
        asyncio.create_task(_hold(limiter, "document", log, "d1", gates[1])),
        asyncio.create_task(_hold(limiter, "interactive", log, "i1", gates[2])),
        asyncio.create_task(_hold(limiter, "document", log, "d2", gates[3])),
        asyncio.create_task(_hold(limiter, "interactive", log, "i2", gates[4])),
    ]
    await _settle()
    for gate in gates:
        gate.set()
        await _settle()
    await asyncio.gather(first, *waiting)
    assert log == ["d0", "i1", "i2", "d1", "d2"]


async def test_congestion_keeps_one_interactive_slot_and_parks_documents() -> None:
    limiter = PriorityLimiter(3, 2)
    limiter.set_congested(True)
    release = asyncio.Event()
    log: list[str] = []
    tasks = [
        asyncio.create_task(_hold(limiter, "document", log, "d0", release)),
        asyncio.create_task(_hold(limiter, "interactive", log, "i0", release)),
        asyncio.create_task(_hold(limiter, "interactive", log, "i1", release)),
    ]
    await _settle()
    assert log == ["i0"] and limiter.capacity == 1 and limiter.doc_capacity == 0
    limiter.set_congested(False)
    await _settle()
    assert sorted(log) == ["d0", "i0", "i1"]
    release.set()
    await asyncio.gather(*tasks)


async def test_cancelled_waiter_does_not_leak_a_slot() -> None:
    limiter = PriorityLimiter(1, 1)
    release = asyncio.Event()
    log: list[str] = []
    holder = asyncio.create_task(_hold(limiter, "interactive", log, "a", release))
    await _settle()
    waiter = asyncio.create_task(_hold(limiter, "interactive", log, "b", release))
    await _settle()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await holder
    assert limiter.idle and log == ["a"]


async def test_on_demand_hook_runs_on_acquire() -> None:
    calls: list[int] = []
    limiter = PriorityLimiter(2, 1, on_demand=lambda: calls.append(1))
    async with limiter.slot("interactive"):
        pass
    assert calls == [1]
    with pytest.raises(ValueError):
        async with limiter.slot("bulk"):  # type: ignore[arg-type]
            pass
