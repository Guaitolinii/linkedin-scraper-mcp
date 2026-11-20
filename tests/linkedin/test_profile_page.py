"""Tests for the profile-identity reader."""

from __future__ import annotations

from unittest.mock import AsyncMock

from linkedin_scraper_mcp.linkedin import profile_urn as profile_urn_module
from linkedin_scraper_mcp.linkedin.profile_page import ProfilePageReader
from linkedin_scraper_mcp.linkedin.profile_urn import ProfileUrnReader
from linkedin_scraper_mcp.linkedin.session import PageSession


def _reader(page) -> ProfilePageReader:
    """Wire the reader the way the facade does."""
    session = PageSession(page)
    return ProfilePageReader(session, ProfileUrnReader(session).read)


class TestExtractProfileUrn:
    async def test_returns_urn_from_atomic_top_card_snapshot(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": "https://www.linkedin.com/in/testuser/",
                "displayName": "Test User",
                "composeHrefs": [
                    "/messaging/compose/?recipient=ACoAAB&"
                    "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB"
                ],
            }
        )

        result = await _reader(mock_page)._extract_profile_urn()

        assert result == "ACoAAB"
        mock_page.evaluate.assert_awaited_once_with(
            profile_urn_module._PROFILE_MESSAGE_TARGET_JS
        )

    async def test_returns_none_for_ambiguous_top_card_links(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": "https://www.linkedin.com/in/testuser/",
                "displayName": "Test User",
                "composeHrefs": [
                    "/messaging/compose/?recipient=ACoAAB",
                    "/messaging/compose/?recipient=OTHER",
                ],
            }
        )

        result = await _reader(mock_page)._extract_profile_urn()

        assert result is None


class TestReadProfileDisplayName:
    """The display name read off a loaded profile page.

    Nothing else in the suite drives this read: every caller stubs it, so
    without these the program could return the wrong line and stay green.
    """

    async def test_the_heading_is_preferred_over_the_body(self, mock_page):
        mock_page.evaluate = AsyncMock(return_value="  Ada   Lovelace ")

        assert await _reader(mock_page)._read_profile_display_name() == (
            "Ada   Lovelace"
        )

    async def test_a_blank_answer_is_reported_as_no_name(self, mock_page):
        mock_page.evaluate = AsyncMock(return_value="   ")

        assert await _reader(mock_page)._read_profile_display_name() is None

    async def test_a_non_string_answer_is_reported_as_no_name(self, mock_page):
        # What a page whose <main> is missing answers through a driver that
        # returns `null` rather than the empty string the program intends.
        mock_page.evaluate = AsyncMock(return_value=None)

        assert await _reader(mock_page)._read_profile_display_name() is None
