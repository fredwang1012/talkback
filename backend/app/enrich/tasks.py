"""Concurrency helper: run coroutines together; the first exception cancels the siblings and is re-raised as itself."""
import asyncio
from typing import Awaitable, Iterable


async def gather_or_cancel(coros: Iterable[Awaitable]) -> None:
    """Like gather, but a failure cancels everything still running (no orphan calls keep spending)."""
    try:
        async with asyncio.TaskGroup() as tg:
            for c in coros:
                tg.create_task(c)
    except* Exception as eg:
        raise eg.exceptions[0] from None
