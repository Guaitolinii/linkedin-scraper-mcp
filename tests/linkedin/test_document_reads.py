"""Every read and action on a LinkedIn page refuses another site.

Each case drives one reader's own script on a page that is a portal's by the
time it is read, with whatever plausible answer the script would give. A read
that skips ``PageSession.run_on_linkedin`` returns that answer and fails here;
``tests/test_off_linkedin_landing_dom.py`` runs the wrapper itself in a real
browser.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from linkedin_scraper_mcp.core.exceptions import OffLinkedInLandingError
from linkedin_scraper_mcp.core.utils import handle_modal_close
from linkedin_scraper_mcp.linkedin import session as session_module
from linkedin_scraper_mcp.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_scraper_mcp.linkedin.content import PageContentReader
from linkedin_scraper_mcp.linkedin.job_pages import JobPageReader
from linkedin_scraper_mcp.linkedin.navigation import PageNavigator
from linkedin_scraper_mcp.linkedin.person import PersonReader
from linkedin_scraper_mcp.linkedin.profile_page import ProfilePageReader
from linkedin_scraper_mcp.linkedin.session import PageSession
from linkedin_scraper_mcp.linkedin.text import JOB_APPLY_EN_US

from .support.navigation import held_in

PORTAL_URL = "https://portal.invalid/interstitial"

Read = Callable[[Any], Awaitable[Any]]


def _session(page: Any) -> PageSession:
    return PageSession(page)


def _content(page: Any) -> PageContentReader:
    return PageContentReader(_session(page))


def _jobs(page: Any) -> JobPageReader:
    session = _session(page)
    return JobPageReader(session, PageNavigator(session), PageContentReader(session))


def _profile_page(page: Any) -> ProfilePageReader:
    return ProfilePageReader(_session(page), AsyncMock())


def _person(page: Any) -> PersonReader:
    session = _session(page)
    navigator = PageNavigator(session)
    return PersonReader(
        session,
        navigator,
        SectionCapture(session, navigator, PageContentReader(session)),
        ProfilePageReader(session, AsyncMock()),
    )


async def _apply_link(page: Any) -> Any:
    with patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock):
        return await _jobs(page).read_apply_link(
            "https://www.linkedin.com/jobs/view/123/", "123", JOB_APPLY_EN_US
        )


CASES: list[tuple[str, Read, Any]] = [
    ("page text", lambda p: _content(p).get_page_text(), "Portal text"),
    (
        "root content",
        lambda p: _content(p)._extract_root_content(["main"]),
        {"source": "root", "text": "Portal text", "references": []},
    ),
    (
        "apply signals",
        _apply_link,
        {"applied": False, "closed": False, "easy_apply": True, "external_link": None},
    ),
    (
        "job ids",
        lambda p: _jobs(p)._extract_job_ids(),
        {"ids": ["123"], "scoped": False},
    ),
    (
        "promoted job ids",
        lambda p: _jobs(p)._extract_promoted_job_ids("Promoted"),
        ["123"],
    ),
    (
        "results above the notice",
        lambda p: _jobs(p)._count_organic_before_notice("Notice", "Promoted"),
        0,
    ),
    ("search page count", lambda p: _jobs(p)._get_total_search_pages(), "1 of 9"),
    ("saved page count", lambda p: _jobs(p)._get_total_list_pages(), 9),
    (
        "profile display name",
        lambda p: _profile_page(p)._read_profile_display_name(),
        "Portal User",
    ),
]


@pytest.mark.parametrize(
    ("read", "answer"),
    [(read, answer) for _name, read, answer in CASES],
    ids=[name for name, _read, _answer in CASES],
)
async def test_a_read_on_another_sites_page_is_refused(
    mock_page, read: Read, answer: Any
):
    mock_page.url = PORTAL_URL
    if answer is not None:
        mock_page.evaluate = AsyncMock(return_value=answer)

    with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
        await read(mock_page)


# Actions. A page script that clicks runs through `run_on_linkedin`, which
# refuses before the script runs; here the double cannot run a script, so the
# driver's address stands in and a bypass returns the click's answer instead
# of raising. A locator action resolves one handle first and asks that handle
# for its own document, so those doubles hold their element on the portal
# while the driver still reports LinkedIn: the handle, not the driver, decides.

LINKEDIN_URL = "https://www.linkedin.com/in/testuser/"


def _portal_element() -> MagicMock:
    element = held_in(MagicMock(), PORTAL_URL)
    for action in ("click", "fill", "press", "scroll_into_view_if_needed"):
        setattr(element, action, AsyncMock())
    element.count = AsyncMock(return_value=3)
    element.is_visible = AsyncMock(return_value=True)
    element.wait_for = AsyncMock()
    element.inner_text = AsyncMock(return_value="FOREIGN PORTAL MESSAGE")
    element.first = element
    element.filter = MagicMock(return_value=element)
    element.locator = MagicMock(return_value=element)
    element.nth = MagicMock(return_value=element)
    return element


async def _show_more(page: Any) -> Any:
    # The driver still says LinkedIn, so the content read after the loop would
    # succeed: only the handle's refusal, carried out of the loop, can fail it.
    page.url = LINKEDIN_URL
    session = _session(page)
    capture = SectionCapture(
        session, PageNavigator(session), PageContentReader(session)
    )
    with (
        patch.object(session_module, "scroll_to_bottom", new_callable=AsyncMock),
        patch.object(session_module, "detect_rate_limit", new_callable=AsyncMock),
        patch.object(session_module, "handle_modal_close", new_callable=AsyncMock),
    ):
        return await capture._extract_loaded_section(
            "https://www.linkedin.com/in/testuser/details/experience/",
            "experience",
            CapturePlan(CaptureMode.DETAILS),
        )


ACTIONS: list[tuple[str, Read]] = [
    ("click a button by its text", lambda p: _content(p).click_button_by_text("Go")),
    ("click a details Show more", _show_more),
    ("close a modal", lambda p: handle_modal_close(p)),
]


@pytest.mark.parametrize(
    "act", [act for _name, act in ACTIONS], ids=[name for name, _act in ACTIONS]
)
async def test_an_action_on_another_sites_page_is_refused_unperformed(
    mock_page, act: Read
):
    element = _portal_element()
    mock_page.element = element
    mock_page.locator = MagicMock(return_value=element)
    # The focused element, for an action pressed where focus is.
    mock_page.evaluate_handle = AsyncMock(return_value=element)
    mock_page.url = PORTAL_URL
    mock_page.evaluate = AsyncMock(return_value=True)

    with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
        await act(mock_page)

    for action in ("click", "fill", "press"):
        getattr(element, action).assert_not_awaited()
