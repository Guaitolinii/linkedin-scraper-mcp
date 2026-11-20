"""Person profile, own-profile, sidebar and people-search workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import logging
import re


from linkedin_scraper_mcp.core.exceptions import LinkedInOperationError
from linkedin_scraper_mcp.error_diagnostics import build_issue_diagnostics
from linkedin_scraper_mcp.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_scraper_mcp.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    rate_limited_section_error,
)
from linkedin_scraper_mcp.linkedin.fields import PERSON_SECTIONS, _person_section_specs
from linkedin_scraper_mcp.linkedin.identifiers import (
    normalize_person_identifier,
    person_profile_url,
)
from linkedin_scraper_mcp.linkedin.link_metadata import Reference
from linkedin_scraper_mcp.linkedin.navigation import PageNavigator
from linkedin_scraper_mcp.linkedin.profile_page import ProfilePageReader
from linkedin_scraper_mcp.linkedin.search_urls import build_people_search_url
from linkedin_scraper_mcp.linkedin.session import NAV_DELAY, PageSession

if TYPE_CHECKING:
    from linkedin_scraper_mcp.callbacks import ProgressCallback

logger = logging.getLogger(__name__)


class PersonReader:
    """Own every workflow whose subject is one LinkedIn member."""

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        capture: SectionCapture,
        profile_page: ProfilePageReader,
    ):
        self._session = session
        self._navigator = navigator
        self._capture = capture
        self._profile_page = profile_page

    async def read_person(
        self,
        username: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
        *,
        main_profile_already_loaded: bool = False,
        allow_self_alias: bool = False,
    ) -> dict[str, Any]:
        """Read a person profile with configurable sections.

        When ``main_profile_already_loaded`` is True and the bound page is on
        the exact profile root for ``username``, the ``main_profile`` section
        is extracted from the current page without re-navigating. Falls back
        to ``extract_page`` if the URL drifts or the reuse path returns the
        soft-rate-limit sentinel (preserving the retry semantics of
        ``extract_page``).

        Returns:
            {url, sections: {name: text}, profile_urn?: str}
        """
        requested = requested | {"main_profile"}
        username = normalize_person_identifier(
            username, allow_self_alias=allow_self_alias
        )
        base_url = person_profile_url(username)
        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        profile_urn: str | None = None
        rate_limited = False

        requested_ordered = [
            spec
            for spec in _person_section_specs(PERSON_SECTIONS, max_scrolls)
            if spec.name in requested
        ]
        total = len(requested_ordered)

        if callbacks:
            await callbacks.on_start("person profile", base_url)

        try:
            for i, spec in enumerate(requested_ordered):
                if i > 0:
                    await self._session.delay(NAV_DELAY)

                section_name = spec.name
                url = base_url + spec.suffix
                try:
                    can_reuse_main = (
                        section_name == "main_profile"
                        and main_profile_already_loaded
                        and urlparse(self._session.page.url).path.rstrip("/")
                        == urlparse(base_url).path.rstrip("/")
                    )
                    if can_reuse_main:
                        extracted = await self._capture._extract_loaded_section(
                            url,
                            section_name=section_name,
                            plan=spec.plan,
                        )
                        if extracted.text == RATE_LIMITED_SECTION_TEXT:
                            logger.info(
                                "Reuse path soft-rate-limited; falling back "
                                "to extract_page for retry parity"
                            )
                            extracted = await self._capture.capture(
                                url,
                                section_name=section_name,
                                plan=spec.plan,
                            )
                    elif CaptureMode.OVERLAY in spec.plan.mode:
                        extracted = await self._capture._extract_overlay(
                            url,
                            section_name=section_name,
                            plan=spec.plan,
                        )
                    else:
                        extracted = await self._capture.capture(
                            url,
                            section_name=section_name,
                            plan=spec.plan,
                        )

                    if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
                        sections[section_name] = extracted.text
                        if extracted.references:
                            references[section_name] = extracted.references
                    elif extracted.text == RATE_LIMITED_SECTION_TEXT:
                        section_errors[section_name] = rate_limited_section_error()
                        # Stop rather than walk the remaining sections. Each one
                        # is another navigation, and LinkedIn has just said it
                        # wants fewer of them. Whatever was gathered before this
                        # point is kept and returned.
                        rate_limited = True
                    elif extracted.error:
                        section_errors[section_name] = extracted.error

                    # Skipped once the section came back empty: there is no
                    # content to read a URN from, and a failure here lands in
                    # the handler below, which would overwrite the entry just
                    # recorded with a generic diagnostic — losing the one
                    # finding this section had.
                    if (
                        section_name == "main_profile"
                        and profile_urn is None
                        and not rate_limited
                    ):
                        profile_urn = await self._profile_page._extract_profile_urn()
                except LinkedInOperationError:
                    raise
                except Exception as e:
                    logger.warning("Error reading section %s: %s", section_name, e)
                    section_errors[section_name] = build_issue_diagnostics(
                        e,
                        context="read_person",
                        target_url=url,
                        section_name=section_name,
                    )

                # "Read" = processed/attempted, not necessarily successful.
                # Per-section failures are captured in section_errors.
                if callbacks:
                    percent = round((i + 1) / total * 95)
                    await callbacks.on_progress(
                        f"Read {section_name} ({i + 1}/{total})", percent
                    )

                if rate_limited:
                    break
        except LinkedInOperationError as e:
            if callbacks:
                await callbacks.on_error(e)
            raise

        result: dict[str, Any] = {
            "url": f"{base_url}/",
            "sections": sections,
        }
        if profile_urn:
            result["profile_urn"] = profile_urn
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors

        if callbacks:
            await callbacks.on_complete("person profile", result)

        return result

    async def get_my_profile(
        self,
        sections: set[str] | None = None,
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """Read the authenticated user's own LinkedIn profile.

        Navigates to /in/me/ and resolves the redirect to obtain the real
        username before reading, so result["url"] reflects the actual profile
        URL rather than /in/me/.

        Returns:
            {url, sections: {name: text}}
        """
        await self._navigator._navigate_to_page("https://www.linkedin.com/in/me/")
        real_url = self._session.page.url  # post-redirect, e.g. /in/johndoe/
        match = re.search(r"/in/([^/?#]+)", real_url)
        username = match.group(1) if match else "me"
        logger.debug("get_my_profile resolved username=%r from %s", username, real_url)

        return await self.read_person(
            username,
            sections if sections is not None else {"main_profile"},
            callbacks=callbacks,
            max_scrolls=max_scrolls,
            main_profile_already_loaded=True,
            # The redirect is what resolves the alias. When it has not, this is
            # still the tool the user asked for, so "me" stays usable here and
            # nowhere else.
            allow_self_alias=True,
        )

    async def search_people(
        self,
        keywords: str,
        location: str | None = None,
        network: list[str] | None = None,
        current_company: str | None = None,
        connection_of: str | None = None,
    ) -> dict[str, Any]:
        """Search for people and extract the results page.

        Args:
            keywords: Free-text query ("software engineer", "recruiter at Google").
            location: Optional location filter. LinkedIn's ``geoUrn`` facet
                only filters on the numeric geo URN id (e.g. ``"103644278"``
                for the United States); plain-text place names are accepted by
                the URL but ignored by LinkedIn and return the unfiltered
                result set, so they raise ``FilterValidationError`` here.
            network: Optional connection-degree filter. Each element is one of
                ``"F"`` (1st-degree), ``"S"`` (2nd-degree), ``"O"`` (3rd-degree
                and beyond). Example: ``["F"]`` to only return 1st-degree
                connections. Invalid tokens raise ``ValueError``. The container
                shape is repaired at the MCP tool boundary, so the list arrives
                here already normalized.
            current_company: Optional current-employer filter. LinkedIn's
                ``currentCompany`` facet only filters on the numeric company
                URN id (e.g. ``"1115"`` for SAP); plain company names are
                accepted by the URL but ignored by LinkedIn and return the
                unfiltered result set. Look up a company's URN via
                ``get_company_profile`` -- it is exposed under
                ``references["about"]``.
            connection_of: Optional profile id (``"ACoAAB..."``, as
                ``get_person_profile`` returns it in ``profile_urn``). Limits
                results to that member's connections; with ``network=["F"]``
                that is the mutual connections. LinkedIn drops the facet
                silently when the member is 3rd-degree or beyond, returning
                the unfiltered network rather than an empty list.

        Returns:
            {url, sections: {name: text}}
        """
        # Builds before it navigates, and the builder refuses a filter
        # LinkedIn would swallow, so an invalid token costs no page load.
        url = build_people_search_url(
            keywords,
            location=location,
            network=network,
            current_company=current_company,
            connection_of=connection_of,
        )
        extracted = await self._capture.capture(
            url,
            section_name="search_results",
            plan=CapturePlan(CaptureMode.SEARCH_RESULTS),
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["search_results"] = extracted.text
            if extracted.references:
                references["search_results"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["search_results"] = rate_limited_section_error()
        elif extracted.error:
            section_errors["search_results"] = extracted.error

        result: dict[str, Any] = {
            "url": url,
            "sections": sections,
        }
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result
