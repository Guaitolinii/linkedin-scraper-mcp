"""Public extractor facade contracts frozen before decomposition."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import inspect

import pytest

from fastmcp.tools import FunctionTool
from patchright.async_api import Page

from linkedin_scraper_mcp import dependencies
from linkedin_scraper_mcp.core.exceptions import InvalidReferenceError
from linkedin_scraper_mcp.linkedin import LinkedInExtractor
from linkedin_scraper_mcp.linkedin import contracts, text
from linkedin_scraper_mcp.linkedin.capture import SectionCapture
from linkedin_scraper_mcp.linkedin.extractor import (
    ExtractedSection,
    FilterValidationError,
    rate_limited_section_error,
    strip_conversation_chrome,
    strip_linkedin_noise,
)
from linkedin_scraper_mcp.linkedin.jobs import JobReader
from linkedin_scraper_mcp.linkedin.profile_page import ProfilePageReader
from linkedin_scraper_mcp.linkedin.profile_urn import ProfileUrnReader
from linkedin_scraper_mcp.server import create_mcp_server

from .policy_scenarios import COMPATIBILITY_METHODS, TOOL_FACADE_METHODS
from .support.policy_trace import ScriptedPage, TraceRecorder


TOOL_DELEGATES = {
    "get_company_employees": "get_company_employees",
    "get_company_posts": "extract_page",
    "get_company_profile": "read_company",
    "get_job_apply_url": "get_job_apply_url",
    "get_job_details": "read_job",
    "get_my_profile": "get_my_profile",
    "get_person_profile": "read_person",
    "get_saved_jobs": "get_saved_jobs",
    "search_companies": "search_companies",
    "search_jobs": "search_jobs",
    "search_people": "search_people",
}


async def test_constructor_export_and_dependency_use_the_same_facade(monkeypatch):
    recorder = TraceRecorder("facade-construction", set())
    page = ScriptedPage(recorder)
    extractor = LinkedInExtractor(cast(Page, page))
    calls: list[str] = []

    async def ready(tool_name: str, ctx: object) -> None:
        assert tool_name == "policy-test"
        assert ctx is None
        calls.append("ready")

    async def browser() -> SimpleNamespace:
        calls.append("browser")
        return SimpleNamespace(page=page)

    async def authenticated() -> None:
        calls.append("authenticated")

    monkeypatch.setattr(dependencies, "ensure_tool_ready_or_raise", ready)
    monkeypatch.setattr(dependencies, "get_or_create_browser", browser)
    monkeypatch.setattr(dependencies, "ensure_authenticated", authenticated)

    constructed = await dependencies.get_ready_extractor(None, tool_name="policy-test")

    expected_state = {
        "_capture",
        "_company",
        "_content",
        "_jobs",
        "_person",
    }
    assert set(vars(extractor)) == expected_state
    assert type(constructed) is LinkedInExtractor
    assert set(vars(constructed)) == expected_state
    assert calls == ["ready", "browser", "authenticated"]


def test_permanent_facade_aliases_are_the_canonical_objects():
    # Identity, not equality: an alias that merely compares equal would let a
    # second copy of a contract live in the facade, and `except
    # FilterValidationError` imported from one copy would not catch the other.
    assert ExtractedSection is contracts.ExtractedSection
    assert FilterValidationError is contracts.FilterValidationError
    assert rate_limited_section_error is contracts.rate_limited_section_error
    assert strip_linkedin_noise is text.strip_linkedin_noise
    assert strip_conversation_chrome is text.strip_conversation_chrome


async def test_registered_tools_match_extractor_delegates():
    tools = await create_mcp_server().list_tools()
    tool_names = {tool.name for tool in tools}

    # The two session tools read no LinkedIn page, so neither has a delegate.
    session_tools = {"close_session", "get_session_status"}
    assert tool_names == {*TOOL_DELEGATES, *session_tools}
    assert set(TOOL_DELEGATES.values()) == TOOL_FACADE_METHODS
    assert not session_tools & set(TOOL_DELEGATES)


async def test_company_posts_delegate_matches_registered_tool_consumer():
    tool = await create_mcp_server().get_tool("get_company_posts")
    assert isinstance(tool, FunctionTool)
    extractor = SimpleNamespace(
        extract_page=AsyncMock(
            return_value=SimpleNamespace(text="posts", references=[], error=None)
        ),
        read_company=AsyncMock(),
    )
    context = SimpleNamespace(report_progress=AsyncMock())

    with patch(
        "linkedin_scraper_mcp.tools.company.get_ready_extractor",
        AsyncMock(return_value=extractor),
    ):
        await tool.fn("example", context)

    delegate = getattr(extractor, TOOL_DELEGATES["get_company_posts"])
    delegate.assert_awaited_once()
    extractor.extract_page.assert_awaited_once_with(
        "https://www.linkedin.com/company/example/posts/",
        section_name="posts",
        max_scrolls=None,
    )
    extractor.read_company.assert_not_awaited()


async def test_facade_read_person_forwards_its_keyword_only_arguments(mock_page):
    # No production caller passes either one to the facade any more: the
    # redirect path that drove `allow_self_alias` through it moved to the
    # person owner, which calls its own `read_person`. Replacing both
    # forwards with `False` survived the whole suite, so the delegate is
    # pinned here, against the real owner rather than a mock of it.
    mock_page.url = "https://www.linkedin.com/in/me/"
    extractor = LinkedInExtractor(cast(Page, mock_page))
    section = ExtractedSection(text="reused", references=[], error=None)

    with (
        patch.object(
            SectionCapture,
            "_extract_loaded_section",
            new_callable=AsyncMock,
            return_value=section,
        ) as loaded,
        patch.object(
            SectionCapture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=section,
        ) as extract_page,
    ):
        result = await extractor.read_person(
            "me",
            {"main_profile"},
            main_profile_already_loaded=True,
            allow_self_alias=True,
        )

    # Reaching this line at all is what `allow_self_alias` buys; reusing the
    # loaded page instead of navigating is what the other one buys.
    assert result["url"] == "https://www.linkedin.com/in/me/"
    loaded.assert_awaited_once()
    extract_page.assert_not_awaited()


async def test_profile_urn_read_is_wired_to_the_urn_reader(mock_page):
    # The reader is bound when the facade is built, so the replacement goes in
    # first. The URN a profile read reports comes from this reader and nothing
    # else.
    section = ExtractedSection(text="Target profile", references=[], error=None)

    with (
        patch.object(
            ProfileUrnReader,
            "read",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(
                target=SimpleNamespace(profile_urn="ACoAReplacement")
            ),
        ) as replacement,
        patch.object(
            SectionCapture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=section,
        ),
    ):
        extractor = LinkedInExtractor(cast(Page, mock_page))
        result = await extractor.read_person("target", {"main_profile"})

    assert result["profile_urn"] == "ACoAReplacement"
    replacement.assert_awaited_once_with()


def test_profile_urn_callback_does_not_retain_the_facade(mock_page):
    with patch.object(ProfilePageReader, "__init__", return_value=None) as construct:
        extractor = LinkedInExtractor(cast(Page, mock_page))

    callback = construct.call_args.args[1]
    captured = inspect.getclosurevars(callback).nonlocals

    assert extractor not in captured.values()
    assert isinstance(callback.__self__, ProfileUrnReader)
    assert extractor not in vars(callback.__self__).values()


async def test_facade_search_jobs_forwards_every_filter_in_order(mock_page):
    # Several filters have the same type, so a positional swap is valid Python
    # and changes the query silently. That exact mutation survived the full
    # suite before this boundary assertion was added.
    extractor = LinkedInExtractor(cast(Page, mock_page))
    expected = {"url": "https://www.linkedin.com/jobs/search/", "sections": {}}

    with patch.object(
        JobReader,
        "search_jobs",
        new_callable=AsyncMock,
        return_value=expected,
    ) as search_jobs:
        result = await extractor.search_jobs(
            "python",
            "Berlin",
            2,
            "past-week",
            "full-time",
            "mid-senior",
            "remote",
            True,
            "recent",
            17.5,
        )

    assert result is expected
    search_jobs.assert_awaited_once_with(
        "python",
        "Berlin",
        2,
        "past-week",
        "full-time",
        "mid-senior",
        "remote",
        True,
        "recent",
        17.5,
    )


async def test_facade_read_person_keeps_refusing_the_self_alias_by_default(mock_page):
    # The other half of the forward above: without the argument `me` is a
    # reserved name, so the assertion that it read the profile says something.
    extractor = LinkedInExtractor(cast(Page, mock_page))

    with pytest.raises(InvalidReferenceError):
        await extractor.read_person("me", {"main_profile"})


def test_facade_methods_are_exactly_the_frozen_coroutine_surface():
    expected = TOOL_FACADE_METHODS | COMPATIBILITY_METHODS
    # Enumerate the class, not the expectation: iterating over `expected` would
    # let a newly added public coroutine pass unnoticed.
    actual = {
        name
        for name in dir(LinkedInExtractor)
        if not name.startswith("_")
        and inspect.iscoroutinefunction(getattr(LinkedInExtractor, name))
    }

    assert actual == expected


async def test_compatibility_helpers_keep_their_browser_behavior():
    allowed = {
        "evaluate",
        "handle.click",
        "handle.dispose",
        "handle.evaluate",
        "handle.scroll_into_view",
        "locator.count",
        "locator.create",
        "locator.derive",
        "locator.element_handle",
    }
    recorder = TraceRecorder("compatibility-helpers", allowed)
    # A LinkedIn address, because the read refuses any other page.
    page = ScriptedPage(recorder, url="https://www.linkedin.com/feed/").script(
        "evaluate:page_text", "Policy text"
    )
    page.declare_locator("main", "main-scope")
    page.declare_locator(
        "button, a, [role='button']", "click-candidates", parent="main-scope"
    )
    page.declare_derived(
        "click-candidates", "filter:^Connect$/re.UNICODE", "exact-connect"
    )
    page.declare_derived("exact-connect", "first", "connect-target")
    page.script("exact-connect.count", 1)
    extractor = LinkedInExtractor(cast(Page, page))

    assert await extractor.get_page_text() == "Policy text"
    assert await extractor.click_button_by_text("Connect") is True
    assert [event["kind"] for event in recorder.events] == [
        "evaluate",
        "locator.create",
        "locator.create",
        "locator.derive",
        "locator.count",
        "locator.derive",
        # Resolved once, asked for its own document, and acted on through
        # that handle, so a redirect cannot move the click to another page.
        "locator.element_handle",
        "handle.evaluate",
        "handle.scroll_into_view",
        "handle.click",
        "handle.dispose",
    ]
