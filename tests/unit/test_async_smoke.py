"""The async plumbing smoke test.

`asyncio_mode = strict` means an `async def` test needs an explicit marker. This file is the
proof that the marker path, the shared async fixture and the event loop all work — the
FastAPI shell and the pool fixtures land on top of it.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_event_loop_is_running(running_loop: asyncio.AbstractEventLoop) -> None:
    assert running_loop.is_running()
    assert running_loop is asyncio.get_running_loop()


@pytest.mark.asyncio
async def test_await_round_trip() -> None:
    async def echo(value: str) -> str:
        await asyncio.sleep(0)
        return value

    assert await echo("ok") == "ok"
