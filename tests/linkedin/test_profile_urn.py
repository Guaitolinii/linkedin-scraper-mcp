"""Tests for the read-only profile URN reader.

The browser-DOM suite that used to cover the page script lived with the
message-sending code, which this project does not include. These tests hold the
pure address and identifier rules the URN read depends on, and the reader's
failure answers over a scripted page.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from linkedin_scraper_mcp.core.exceptions import OffLinkedInLandingError
from linkedin_scraper_mcp.linkedin.profile_urn import (
    ProfileUrnReader,
    _normalize_profile_urn,
    _profile_path_from_url,
    _profile_urn_from_compose_url,
    _safe_linkedin_url,
)
from linkedin_scraper_mcp.linkedin.session import PageSession

PROFILE = "https://www.linkedin.com/in/ada-lovelace/"
COMPOSE = (
    "https://www.linkedin.com/messaging/compose/"
    "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB"
)


class TestSafeLinkedinUrl:
    def test_a_plain_linkedin_address_is_kept(self):
        parsed = _safe_linkedin_url(PROFILE)

        assert parsed is not None
        assert parsed.path == "/in/ada-lovelace/"

    def test_a_relative_address_resolves_against_its_base(self):
        parsed = _safe_linkedin_url("/in/ada/", base="https://www.linkedin.com/x")

        assert parsed is not None
        assert parsed.geturl() == "https://www.linkedin.com/in/ada/"

    @pytest.mark.parametrize(
        "value",
        [
            "http://www.linkedin.com/in/ada/",
            "https://user:pass@www.linkedin.com/in/ada/",
            "https://www.linkedin.com.evil.test/in/ada/",
            "https://evil.test/https://www.linkedin.com/in/ada/",
            "https://www.linkedin.com:8443/in/ada/",
            "https://www.linkedin.com/in/ada/#fragment",
            "https://www.linkedin.com\\@evil.test/in/ada/",
            "https://www.linkedin.com/in/a\nda/",
            "",
            "   ",
        ],
    )
    def test_anything_else_is_refused(self, value: str):
        assert _safe_linkedin_url(value) is None


class TestNormalizeProfileUrn:
    @pytest.mark.parametrize(
        "value",
        ["ACoAAB", "urn:li:fsd_profile:ACoAAB", "  ACoAAB  "],
    )
    def test_a_raw_id_and_a_full_urn_name_the_same_member(self, value: str):
        assert _normalize_profile_urn(value) == "ACoAAB"

    @pytest.mark.parametrize(
        "value",
        ["urn:li:company:123", "/feed/", "AC oAAB", "", None],
    )
    def test_anything_that_is_not_a_profile_id_is_refused(self, value):
        assert _normalize_profile_urn(value) is None


class TestProfilePathFromUrl:
    def test_a_profile_page_gives_its_canonical_path(self):
        assert _profile_path_from_url(PROFILE) == "/in/ada-lovelace/"

    def test_the_self_profile_flag_is_the_only_query_allowed(self):
        assert _profile_path_from_url(f"{PROFILE}?isSelfProfile=false") == (
            "/in/ada-lovelace/"
        )
        assert _profile_path_from_url(f"{PROFILE}?utm=1") is None

    @pytest.mark.parametrize(
        "value",
        [
            "https://www.linkedin.com/company/acme/",
            "https://www.linkedin.com/in/ada-lovelace/details/experience/",
            "https://www.linkedin.com/feed/",
            "https://evil.test/in/ada-lovelace/",
        ],
    )
    def test_any_other_page_is_not_a_profile(self, value: str):
        assert _profile_path_from_url(value) is None


class TestProfileUrnFromComposeUrl:
    def test_the_recipient_and_the_urn_agree(self):
        assert _profile_urn_from_compose_url(COMPOSE) == "ACoAAB"

    def test_a_recipient_alone_is_enough(self):
        compose = "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB"

        assert _profile_urn_from_compose_url(compose) == "ACoAAB"

    def test_a_relative_link_resolves_against_the_profile_page(self):
        assert (
            _profile_urn_from_compose_url(
                "/messaging/compose/?recipient=ACoAAB", base=PROFILE
            )
            == "ACoAAB"
        )

    @pytest.mark.parametrize(
        "value",
        [
            # Two different members named by one link.
            "https://www.linkedin.com/messaging/compose/"
            "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AOTHER",
            # Not the compose route.
            "https://www.linkedin.com/feed/?recipient=ACoAAB",
            # Nobody named at all.
            "https://www.linkedin.com/messaging/compose/",
            # Another site.
            "https://evil.test/messaging/compose/?recipient=ACoAAB",
        ],
    )
    def test_an_ambiguous_or_foreign_link_names_nobody(self, value: str):
        assert _profile_urn_from_compose_url(value) is None


class TestProfileUrnReader:
    @staticmethod
    def _reader(page) -> ProfileUrnReader:
        return ProfileUrnReader(PageSession(page))

    async def test_a_page_script_that_raises_is_a_failed_read(self, mock_page):
        mock_page.evaluate = AsyncMock(side_effect=RuntimeError("context destroyed"))

        resolution = await self._reader(mock_page).read()

        assert resolution.status == "failed"
        assert resolution.target is None

    async def test_an_answer_that_is_not_a_mapping_is_a_failed_read(self, mock_page):
        mock_page.evaluate = AsyncMock(return_value=None)

        assert (await self._reader(mock_page).read()).status == "failed"

    async def test_a_profile_without_a_message_action_is_unavailable(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={"status": "unavailable", "pageUrl": PROFILE}
        )

        resolution = await self._reader(mock_page).read()

        assert resolution.status == "unavailable"
        assert resolution.target is None

    async def test_an_unavailable_answer_off_a_profile_is_a_failed_read(
        self, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "unavailable",
                "pageUrl": "https://www.linkedin.com/feed/",
            }
        )

        assert (await self._reader(mock_page).read()).status == "failed"

    async def test_a_resolved_answer_yields_the_target(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": PROFILE,
                "displayName": "  Ada Lovelace ",
                "composeHrefs": [COMPOSE],
            }
        )

        resolution = await self._reader(mock_page).read()

        assert resolution.status == "resolved"
        assert resolution.target is not None
        assert resolution.target.profile_urn == "ACoAAB"
        assert resolution.target.profile_path == "/in/ada-lovelace/"
        assert resolution.target.display_name == "Ada Lovelace"

    async def test_two_compose_links_are_not_guessed_between(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": PROFILE,
                "displayName": "Ada Lovelace",
                "composeHrefs": [
                    COMPOSE,
                    "https://www.linkedin.com/messaging/compose/?recipient=OTHER",
                ],
            }
        )

        assert (await self._reader(mock_page).read()).status == "failed"

    async def test_an_answer_from_another_site_is_refused_by_name(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": "https://portal.invalid/interstitial",
                "displayName": "Ada Lovelace",
                "composeHrefs": [COMPOSE],
            }
        )

        with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
            await self._reader(mock_page).read()
