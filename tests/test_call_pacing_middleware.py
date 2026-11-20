"""The browser lock paces calls, and only ever delays them."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_scraper_mcp.call_pacing import CallPacer
from linkedin_scraper_mcp.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)


def _context(name: str = "search_jobs"):
    return SimpleNamespace(message=SimpleNamespace(name=name), fastmcp_context=None)


def _middleware(pacer: CallPacer) -> SequentialToolExecutionMiddleware:
    middleware = SequentialToolExecutionMiddleware()
    middleware._pacer = pacer
    # The profile and browser plumbing is not what is under test.
    middleware._run_owning_the_profile = AsyncMock(return_value="result")
    return middleware


def test_a_paced_call_waits_then_runs_and_marks_the_end():
    pacer = MagicMock(spec=CallPacer)
    pacer.enabled = True
    pacer.wait = AsyncMock(return_value=2.0)
    middleware = _middleware(pacer)

    result = asyncio.run(middleware.on_call_tool(_context(), AsyncMock()))

    assert result == "result"
    pacer.wait.assert_awaited_once()
    pacer.finished.assert_called_once()


def test_the_end_is_marked_even_when_the_call_fails():
    pacer = MagicMock(spec=CallPacer)
    pacer.enabled = True
    pacer.wait = AsyncMock(return_value=0.0)
    middleware = _middleware(pacer)
    middleware._run_owning_the_profile = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        asyncio.run(middleware.on_call_tool(_context(), AsyncMock()))

    pacer.finished.assert_called_once()


def test_a_disabled_pacer_is_never_consulted():
    pacer = MagicMock(spec=CallPacer)
    pacer.enabled = False
    pacer.wait = AsyncMock()
    middleware = _middleware(pacer)

    asyncio.run(middleware.on_call_tool(_context(), AsyncMock()))

    pacer.wait.assert_not_awaited()


def test_the_session_status_tool_is_not_paced():
    pacer = MagicMock(spec=CallPacer)
    pacer.enabled = True
    pacer.wait = AsyncMock()
    middleware = _middleware(pacer)
    call_next = AsyncMock(return_value="status")

    result = asyncio.run(
        middleware.on_call_tool(_context("get_session_status"), call_next)
    )

    assert result == "status"
    pacer.wait.assert_not_awaited()
