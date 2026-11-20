"""Public page-reading facade and collaborator composition root."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from patchright.async_api import Page

from linkedin_scraper_mcp.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_scraper_mcp.linkedin.capture import SectionCapture
from linkedin_scraper_mcp.linkedin.company import CompanyReader
from linkedin_scraper_mcp.linkedin.content import PageContentReader
from linkedin_scraper_mcp.linkedin.contracts import (
    ExtractedSection as ExtractedSection,
    FilterValidationError as FilterValidationError,
    rate_limited_section_error as rate_limited_section_error,
)
from linkedin_scraper_mcp.linkedin.job_pages import JobPageReader
from linkedin_scraper_mcp.linkedin.job_policy import JobsTrackerStage
from linkedin_scraper_mcp.linkedin.jobs import JobReader
from linkedin_scraper_mcp.linkedin.navigation import PageNavigator
from linkedin_scraper_mcp.linkedin.person import PersonReader
from linkedin_scraper_mcp.linkedin.profile_page import ProfilePageReader
from linkedin_scraper_mcp.linkedin.profile_urn import ProfileUrnReader
from linkedin_scraper_mcp.linkedin.session import PageSession
from linkedin_scraper_mcp.linkedin.text import (
    strip_conversation_chrome as strip_conversation_chrome,
    strip_linkedin_noise as strip_linkedin_noise,
)


if TYPE_CHECKING:
    from linkedin_scraper_mcp.callbacks import ProgressCallback


class LinkedInExtractor:
    """Compose page workflow owners and expose the stable tool-facing API."""

    def __init__(self, page: Page):
        session = PageSession(page)
        navigator = PageNavigator(session)
        content = PageContentReader(session)
        capture = SectionCapture(session, navigator, content)
        profile_page = ProfilePageReader(session, ProfileUrnReader(session).read)
        person = PersonReader(session, navigator, capture, profile_page)

        self._content = content
        self._capture = capture
        self._person = person
        self._company = CompanyReader(session, capture)
        job_pages = JobPageReader(session, navigator, content)
        self._jobs = JobReader(navigator, capture, job_pages)

    async def get_page_text(self) -> str:
        """Extract innerText from the main content area of the current page."""
        return await self._content.get_page_text()

    async def click_button_by_text(
        self, text: str, *, scope: str = "main", timeout: int = 5000
    ) -> bool:
        """Click the first button or link whose visible text exactly matches."""
        return await self._content.click_button_by_text(
            text, scope=scope, timeout=timeout
        )

    async def extract_page(
        self,
        url: str,
        section_name: str,
        max_scrolls: int | None = None,
    ) -> ExtractedSection:
        """Navigate, scroll to load lazy content, and extract innerText."""
        return await self._capture.extract_page(url, section_name, max_scrolls)

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
        """Read a person profile with configurable sections."""
        return await self._person.read_person(
            username,
            requested,
            callbacks,
            max_scrolls,
            main_profile_already_loaded=main_profile_already_loaded,
            allow_self_alias=allow_self_alias,
        )

    async def get_my_profile(
        self,
        sections: set[str] | None = None,
        callbacks: ProgressCallback | None = None,
        max_scrolls: int | None = None,
    ) -> dict[str, Any]:
        """Read the authenticated user's own LinkedIn profile."""
        return await self._person.get_my_profile(sections, callbacks, max_scrolls)

    async def read_company(
        self,
        company_name: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Read a company profile with configurable sections."""
        return await self._company.read_company(company_name, requested, callbacks)

    async def get_company_employees(
        self,
        company_name: str,
        keywords: str | None = None,
    ) -> dict[str, Any]:
        """List employees at a company from the people page."""
        return await self._company.get_company_employees(company_name, keywords)

    async def read_job(self, job_id: str) -> dict[str, Any]:
        """Read a single job posting."""
        return await self._jobs.read_job(job_id)

    async def get_job_apply_url(self, job_id: str) -> dict[str, Any]:
        """Read how a job posting takes applications and the employer's link."""
        return await self._jobs.get_job_apply_url(job_id)

    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Search for jobs with pagination and job ID extraction."""
        return await self._jobs.search_jobs(
            keywords,
            location,
            max_pages,
            date_posted,
            job_type,
            experience_level,
            work_type,
            easy_apply,
            sort_by,
            tool_timeout,
        )

    async def get_saved_jobs(
        self, max_pages: int = 3, stage: JobsTrackerStage = "saved"
    ) -> dict[str, Any]:
        """List the authenticated user's jobs at one job-tracker stage."""
        return await self._jobs.get_saved_jobs(max_pages, stage)

    async def search_people(
        self,
        keywords: str,
        location: str | None = None,
        network: list[str] | None = None,
        current_company: str | None = None,
        connection_of: str | None = None,
    ) -> dict[str, Any]:
        """Search for people and extract the results page."""
        return await self._person.search_people(
            keywords,
            location=location,
            network=network,
            current_company=current_company,
            connection_of=connection_of,
        )

    async def search_companies(self, keywords: str) -> dict[str, Any]:
        """Search for companies and extract the results page."""
        return await self._company.search_companies(keywords)
