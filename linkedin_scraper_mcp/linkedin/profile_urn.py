"""Read the profile URN off a loaded LinkedIn person page.

Read-only. The URN is the member id carried by the href of the profile's own
top-card action link. This module only reads that link: it never opens, clicks
or follows it, and sends nothing.

Modified from linkedin-scraper-mcp (Apache-2.0, Copyright 2025-2026 Daniel
Sticker): this is the profile-URN read split out of its former message-sending
module, which this project does not include.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Literal
from urllib.parse import ParseResult, parse_qs, urljoin, urlparse

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_scraper_mcp.core.destination import raise_if_off_linkedin
from linkedin_scraper_mcp.core.exceptions import LinkedInOperationError
from linkedin_scraper_mcp.linkedin.identifiers import (
    normalize_person_identifier,
    normalize_profile_urn,
    person_profile_url,
)
from linkedin_scraper_mcp.linkedin.session import PageSession

logger = logging.getLogger(__name__)

_PROFILE_MESSAGE_TARGET_JS = r"""() => {
    const visible = element => {
        const visibility = element && getComputedStyle(element).visibility;
        return !!(
            element &&
            visibility !== 'hidden' &&
            visibility !== 'collapse' &&
            (element.offsetWidth || element.offsetHeight || element.getClientRects().length)
        );
    };
    const active = anchor =>
        visible(anchor) &&
        !anchor.hasAttribute('disabled') &&
        (anchor.getAttribute('aria-disabled') || '').toLowerCase() !== 'true';
    const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
    const validComposeHref = value => {
        if (typeof value !== 'string' || /[\\\x00-\x1f\x7f]/.test(value)) {
            return false;
        }
        try {
            const url = new URL(value, window.location.href);
            const hostname = url.hostname.toLowerCase().replace(/\.$/, '');
            if (
                url.protocol !== 'https:' ||
                !/(^|\.)linkedin\.com$/.test(hostname) ||
                url.username ||
                url.password ||
                (url.port && url.port !== '443') ||
                url.hash ||
                url.pathname !== '/messaging/compose/'
            ) {
                return false;
            }
            const values = [
                ...url.searchParams.getAll('recipient'),
                ...url.searchParams.getAll('profileUrn'),
            ];
            const normalized = values.map(item => {
                const text = item.trim();
                const prefix = 'urn:li:fsd_profile:';
                const identifier = text.startsWith(prefix)
                    ? text.slice(prefix.length)
                    : text;
                return /^[A-Za-z0-9_-]+$/.test(identifier) ? identifier : null;
            });
            return normalized.length > 0 &&
                normalized.every(item => item !== null && item === normalized[0]);
        } catch {
            return false;
        }
    };
    const main = document.querySelector('main');
    if (!main) return {status: 'unresolved'};

    // The top card is the first section that wraps no other section. LinkedIn
    // nests it inside a wrapper section and has moved the name between h1 and
    // h2, so neither its depth nor its heading level is pinned. It is chosen
    // before its heading is checked, so a card whose name has not rendered yet
    // stays unresolved instead of yielding to the next section. Sidebar
    // sections are skipped: they carry other people's Message links.
    const section = Array.from(main.querySelectorAll('section')).find(
        element =>
            visible(element) &&
            !element.closest('aside') &&
            !element.querySelector('section')
    );
    if (!section) return {status: 'unresolved'};
    const headings = Array.from(section.querySelectorAll('h1, h2, h3')).filter(visible);
    const visibleComposeAnchors = Array.from(
        section.querySelectorAll('a[href*="/messaging/compose/"]')
    ).filter(anchor => visible(anchor) && anchor.closest('section') === section);
    const composeAnchors = visibleComposeAnchors.filter(active);
    if (
        headings.length !== 1 ||
        composeAnchors.length > 1 ||
        (composeAnchors.length === 1 && visibleComposeAnchors.length !== 1)
    ) {
        return {status: 'unresolved'};
    }
    if (composeAnchors.length === 0) {
        return visibleComposeAnchors.length === 0
            ? {status: 'unavailable', pageUrl: window.location.href}
            : {status: 'unresolved'};
    }

    const anchor = composeAnchors[0];
    const composeHref = anchor.getAttribute('href') || anchor.href || '';
    if (!validComposeHref(composeHref)) return {status: 'unresolved'};
    return {
        status: 'resolved',
        pageUrl: window.location.href,
        displayName: normalize(
            headings[0].innerText || headings[0].textContent || ''
        ),
        composeHrefs: [composeHref],
    };
}"""

_PROFILE_MESSAGE_TARGET_READY_JS = (
    f"() => ({_PROFILE_MESSAGE_TARGET_JS})().status === 'resolved'"
)

_PROFILE_MESSAGE_TARGET_READY_JS = (
    f"() => ({_PROFILE_MESSAGE_TARGET_JS})().status === 'resolved'"
)
_PROFILE_MESSAGE_TARGET_TIMEOUT_MS = 1_000

_LINKEDIN_MESSAGE_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")
_PROFILE_PATH_RE = re.compile(r"^/in/[^/?#]+/$")
_PROFILE_URN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PROFILE_URN_PREFIX = "urn:li:fsd_profile:"


@dataclass(frozen=True)
class _ProfileMessageTarget:
    profile_path: str
    profile_urn: str
    compose_url: str
    display_name: str | None


@dataclass(frozen=True)
class _ProfileMessageTargetResolution:
    status: Literal["resolved", "unavailable", "failed"]
    target: _ProfileMessageTarget | None = None


def _safe_linkedin_url(value: str, *, base: str | None = None) -> ParseResult | None:
    """Parse an HTTPS LinkedIn URL without credentials or an ambiguous origin."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    candidate = urljoin(base, value.strip()) if base else value.strip()
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower().removesuffix(".")
    if (
        parsed.scheme != "https"
        or not _LINKEDIN_MESSAGE_HOST_RE.fullmatch(hostname)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        return None
    return parsed


def _normalize_profile_urn(value: str | None) -> str | None:
    """Return the identifier carried by a profile URN or raw recipient value."""
    if not isinstance(value, str):
        return None
    try:
        candidate = normalize_profile_urn(value)
    except LinkedInOperationError:
        return None
    return candidate.removeprefix(_PROFILE_URN_PREFIX)


# The 2026 profile page lands on /in/<name>/?isSelfProfile=false. That flag is
# the only query a profile route may carry; any other still refuses.
_BENIGN_PROFILE_QUERIES = {"", "isSelfProfile=false"}


def _profile_path_from_url(value: str) -> str | None:
    parsed = _safe_linkedin_url(value)
    if (
        parsed is None
        or parsed.query not in _BENIGN_PROFILE_QUERIES
        or not _PROFILE_PATH_RE.fullmatch(parsed.path)
    ):
        return None
    try:
        username = normalize_person_identifier(parsed._replace(query="").geturl())
    except LinkedInOperationError:
        return None
    canonical_path = urlparse(person_profile_url(username, "/")).path
    return parsed.path if parsed.path == canonical_path else None


def _profile_urn_from_compose_url(value: str, *, base: str | None = None) -> str | None:
    parsed = _safe_linkedin_url(value, base=base)
    if parsed is None or parsed.path != "/messaging/compose/":
        return None
    params = parse_qs(parsed.query, keep_blank_values=True)
    identifiers: set[str] = set()
    for key in ("recipient", "profileUrn"):
        values = params.get(key, [])
        normalized = [_normalize_profile_urn(item) for item in values]
        if any(item is None for item in normalized):
            return None
        identifiers.update(item for item in normalized if item is not None)
    if len(identifiers) != 1:
        return None
    return identifiers.pop()


class ProfileUrnReader:
    """Resolve the profile URN of the person page currently loaded."""

    def __init__(self, session: PageSession):
        self._page = session.page

    async def read(
        self, *, timeout_ms: int = _PROFILE_MESSAGE_TARGET_TIMEOUT_MS
    ) -> _ProfileMessageTargetResolution:
        """Resolve one recipient-specific top-card compose action after settling."""
        try:
            await self._page.wait_for_function(
                _PROFILE_MESSAGE_TARGET_READY_JS,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass
        except Exception:
            logger.debug("Could not wait for the profile Message action", exc_info=True)

        try:
            # Not through `run_on_linkedin`: the script returns its document's
            # own `pageUrl`, and `_profile_path_from_url` below refuses any
            # address but a LinkedIn profile, so another site reads as failed.
            data = await self._page.evaluate(_PROFILE_MESSAGE_TARGET_JS)
        except Exception:
            logger.debug("Could not inspect the profile Message action", exc_info=True)
            return _ProfileMessageTargetResolution("failed")
        if not isinstance(data, dict):
            return _ProfileMessageTargetResolution("failed")
        # Said as the landing it is: read as a failed recipient, a portal that
        # replaced the profile would look like a profile without a Message
        # action, and nothing would name where the browser went.
        if isinstance(data.get("pageUrl"), str):
            raise_if_off_linkedin(data["pageUrl"])
        if data.get("status") == "unavailable":
            page_url = data.get("pageUrl")
            if (
                not isinstance(page_url, str)
                or _profile_path_from_url(page_url) is None
            ):
                return _ProfileMessageTargetResolution("failed")
            return _ProfileMessageTargetResolution("unavailable")
        if data.get("status") != "resolved":
            return _ProfileMessageTargetResolution("failed")

        page_url = data.get("pageUrl")
        compose_hrefs = data.get("composeHrefs")
        if not isinstance(page_url, str) or not isinstance(compose_hrefs, list):
            return _ProfileMessageTargetResolution("failed")
        profile_path = _profile_path_from_url(page_url)
        if profile_path is None:
            return _ProfileMessageTargetResolution("failed")
        if len(compose_hrefs) != 1 or not isinstance(compose_hrefs[0], str):
            return _ProfileMessageTargetResolution("failed")

        parsed_compose = _safe_linkedin_url(compose_hrefs[0], base=page_url)
        if parsed_compose is None:
            return _ProfileMessageTargetResolution("failed")
        compose_url = parsed_compose.geturl()
        profile_urn = _profile_urn_from_compose_url(compose_url)
        if profile_urn is None:
            return _ProfileMessageTargetResolution("failed")

        display_name = data.get("displayName")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = None
        else:
            display_name = display_name.strip()
        return _ProfileMessageTargetResolution(
            "resolved",
            _ProfileMessageTarget(
                profile_path=profile_path,
                profile_urn=profile_urn,
                compose_url=compose_url,
                display_name=display_name,
            ),
        )
