"""Canonical semantic policy-trace scenarios."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from difflib import unified_diff
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import inspect
import json

from patchright.async_api import Page
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_scraper_mcp.callbacks import ProgressCallback
from linkedin_scraper_mcp.linkedin import capture as capture_module
from linkedin_scraper_mcp.linkedin import company as company_module
from linkedin_scraper_mcp.linkedin import job_pages as job_pages_module
from linkedin_scraper_mcp.linkedin import jobs as jobs_module
from linkedin_scraper_mcp.linkedin import navigation as navigation_module
from linkedin_scraper_mcp.linkedin import person as person_module
from linkedin_scraper_mcp.linkedin import session as session_module
from linkedin_scraper_mcp.linkedin import LinkedInExtractor
from linkedin_scraper_mcp.linkedin.fields import COMPANY_SECTIONS, PERSON_SECTIONS
from linkedin_scraper_mcp.server import create_mcp_server

from .support.policy_trace import (
    FakeClock,
    ScriptedPage,
    TraceRecorder,
    bind_effective,
)


ROOT = Path(__file__).parents[2]
TRACE_ROOT = ROOT / "tests" / "fixtures" / "policy-traces" / "v1"
_TOOL_SCHEMAS: dict[str, dict[str, Any]] | None = None

_COMMON_ALLOWED = {
    "boundary.auth",
    "boundary.auth_quick",
    "boundary.drain",
    "boundary.modal",
    "boundary.rate_limit",
    "boundary.scroll_body",
    "boundary.scroll_sidebar",
    "boundary.stabilize",
    "boundary.trace",
    "callback.complete",
    "callback.progress",
    "callback.start",
    "evaluate",
    "evaluate_handle",
    "handle.as_element",
    "handle.dispose",
    "handle.evaluate",
    "handle.json_value",
    "keyboard.press",
    "keyboard.type",
    "listener.add",
    "listener.emit",
    "listener.remove",
    "locator.click",
    "locator.count",
    "locator.create",
    "locator.derive",
    "locator.is_visible",
    "locator.scroll_into_view",
    "locator.wait_for",
    "mouse.move",
    "mouse.wheel",
    "navigate",
    "sleep",
    "wait_for_function",
    "wait_for_load_state",
    "wait_for_selector",
}


class TraceCallbacks(ProgressCallback):
    """Record progress callbacks without a mock object."""

    def __init__(self, recorder: TraceRecorder):
        self.recorder = recorder

    async def on_start(self, subject: str, url: str) -> None:
        self.recorder.record("callback.start", operation=subject, url=url)

    async def on_progress(self, message: str, percent: int) -> None:
        self.recorder.record("callback.progress", message=message, percent=percent)

    async def on_complete(self, subject: str, result: Any) -> None:
        self.recorder.record(
            "callback.complete", operation=subject, result_url=result["url"]
        )


@contextmanager
def _diagnostics_bindings(diagnostics: Any) -> Generator[None]:
    """Bind the issue-report boundary in each exercised linkedin module.

    A separate context manager rather than more items in `boundaries`, which
    sat on exactly 20 and is the whole of CPython's static block budget inside
    an async generator: the twenty-first raised `SyntaxError: too many
    statically nested blocks` at import, before any test ran. Each module
    holds its own name for the function, so patching fewer than all of them
    lets the real one write an issue report for a scripted page.
    """

    with (
        patch.object(capture_module, "build_issue_diagnostics", diagnostics),
        patch.object(person_module, "build_issue_diagnostics", diagnostics),
        patch.object(company_module, "build_issue_diagnostics", diagnostics),
        patch.object(job_pages_module, "build_issue_diagnostics", diagnostics),
        patch.object(jobs_module, "build_issue_diagnostics", diagnostics),
    ):
        yield


@asynccontextmanager
async def boundaries(
    recorder: TraceRecorder,
    clock: FakeClock,
    *,
    auth_result: str | None = None,
) -> AsyncGenerator[None]:
    real_scroll_body = session_module.scroll_to_bottom
    real_scroll_sidebar = session_module.scroll_job_sidebar

    async def trace(_page: Any, label: str, *, extra: Any = None) -> None:
        recorder.record("boundary.trace", label=label, extra=extra)

    async def auth_quick(_page: Any) -> None:
        recorder.record("boundary.auth_quick", result=None)
        return None

    async def auth(_page: Any) -> str | None:
        recorder.record("boundary.auth", result=auth_result)
        return auth_result

    async def remember(_page: Any) -> bool:
        return False

    async def stabilize(description: str, _logger: Any) -> None:
        recorder.record(
            "boundary.stabilize",
            description=description,
            result=None,
        )

    async def rate_limit(_page: Any) -> None:
        recorder.record("boundary.rate_limit")

    async def modal(_page: Any) -> bool:
        recorder.record("boundary.modal", dismissed=False)
        return False

    async def scroll_body(*args: Any, **kwargs: Any) -> None:
        values = bind_effective(real_scroll_body, *args, **kwargs)
        values.pop("page")
        recorder.record("boundary.scroll_body", **values, actual_scrolls=2)
        clock.advance(values["pause_time"] * 2)

    async def scroll_sidebar(*args: Any, **kwargs: Any) -> bool:
        values = bind_effective(real_scroll_sidebar, *args, **kwargs)
        values.pop("page")
        recorder.record(
            "boundary.scroll_sidebar", **values, actual_scrolls=2, moved=False
        )
        clock.advance(0.4)
        return False

    def diagnostics(error: Exception, **values: Any) -> dict[str, Any]:
        return {
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": values.get("context"),
        }

    with (
        patch.object(navigation_module, "record_page_trace", trace),
        patch.object(navigation_module, "detect_auth_barrier_quick", auth_quick),
        patch.object(navigation_module, "detect_auth_barrier", auth),
        patch.object(navigation_module, "resolve_remember_me_prompt", remember),
        patch.object(navigation_module, "stabilize_navigation", stabilize),
        # Every binding of each shared boundary, because the workflows that
        # reach it are split across the modules mid-relocation: generic
        # capture goes through `PageSession`, while the job pages import the helper into
        # `job_pages`. Patching one side only lets the real helper loose on a
        # scripted page. The scrolls have no facade binding left at all — the job
        # reader held the last one — and neither has the modal close, whose
        # last facade caller left with the conversation reader.
        patch.object(session_module, "detect_rate_limit", rate_limit),
        patch.object(job_pages_module, "detect_rate_limit", rate_limit),
        patch.object(session_module, "handle_modal_close", modal),
        patch.object(job_pages_module, "handle_modal_close", modal),
        patch.object(session_module, "scroll_to_bottom", scroll_body),
        patch.object(job_pages_module, "scroll_to_bottom", scroll_body),
        patch.object(session_module, "scroll_job_sidebar", scroll_sidebar),
        patch.object(job_pages_module, "scroll_job_sidebar", scroll_sidebar),
        _diagnostics_bindings(diagnostics),
        patch.object(session_module.asyncio, "sleep", clock.sleep),
        patch.object(session_module.time, "monotonic", clock.monotonic),
    ):
        yield


def _page(recorder: TraceRecorder, *, url: str = "about:blank") -> ScriptedPage:
    return ScriptedPage(recorder, url=url).script("evaluate:root_content")


def _extractor(page: ScriptedPage) -> LinkedInExtractor:
    return LinkedInExtractor(cast(Page, page))


def _complete_mapping_result(result: dict[str, Any], **derived: Any) -> dict[str, Any]:
    overlap = result.keys() & derived.keys()
    if overlap:
        raise AssertionError(
            f"derived result fields overlap raw result: {sorted(overlap)}"
        )
    return {**result, **derived}


def _root(text: str, references: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "source": "root",
        "text": text,
        "references": references or [],
    }


async def _generic_capture_scenario(
    name: str, url: str, *, max_scrolls: int | None = None
) -> dict[str, Any]:
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script("evaluate:root_content", _root("Policy content"))
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("extract_page", "section"):
            result = await extractor.extract_page(
                url, "section", max_scrolls=max_scrolls
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "extract_page",
            "arguments": {
                "url": url,
                "section_name": "section",
                "max_scrolls": max_scrolls,
            },
        },
        {"text": result.text, "references": result.references},
    )


async def _person_sections_scenario() -> dict[str, Any]:
    name = "read_person__all_sections"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    # 13 valid company anchors against the documented cap of 12 for a section,
    # so dropping the cap shows up as a fourteenth reference in the trace.
    overflowing = [
        {
            "href": f"https://www.linkedin.com/company/policy-employer-{index}/",
            "text": f"Employer {index}",
        }
        for index in range(13)
    ]
    roots = [
        _root(
            f"{section} content",
            overflowing if section == "experience" else None,
        )
        for section in PERSON_SECTIONS
    ]
    page.script("evaluate:root_content", *roots)
    _script_profile_target(page)
    page.declare_locator("main button", "show_more")
    page.declare_derived(
        "show_more",
        "filter:^Show (more|all)\\b/re.IGNORECASE|re.UNICODE",
        "show_more.filtered",
    )
    page.script("show_more.filtered.count", *([0] * 9))
    extractor = _extractor(page)
    callbacks = TraceCallbacks(recorder)
    async with boundaries(recorder, clock):
        with recorder.context("read_person"):
            result = await extractor.read_person(
                "ada-lovelace", set(PERSON_SECTIONS), callbacks=callbacks
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "read_person",
            "arguments": {
                "username": "ada-lovelace",
                "requested": list(PERSON_SECTIONS),
            },
        },
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _company_sections_scenario() -> dict[str, Any]:
    name = "read_company__all_sections"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:root_content",
        *[_root(f"{section} content") for section in COMPANY_SECTIONS],
    )
    extractor = _extractor(page)
    callbacks = TraceCallbacks(recorder)
    async with boundaries(recorder, clock):
        with recorder.context("read_company"):
            result = await extractor.read_company(
                "analytical-engine", set(COMPANY_SECTIONS), callbacks=callbacks
            )
    page.assert_clean()
    return recorder.trace(
        {
            "method": "read_company",
            "arguments": {
                "company_name": "analytical-engine",
                "requested": list(COMPANY_SECTIONS),
            },
        },
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _job_search_scenario(route: str = "/jobs/search/") -> dict[str, Any]:
    is_alias = "search-results" in route
    name = "search_jobs__route_alias" if is_alias else "search_jobs__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append(f"https://www.linkedin.com{route}?keywords=python")
    ids = ["101", "102"] if is_alias else [str(101 + index) for index in range(16)]
    references = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Senior policy engineer",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/company/company-{index}/",
                "text": f"Company {index}",
                "heading": "",
            }
            for index in range(20)
        ],
    ]
    page.script("evaluate:root_content", _root("Python jobs\n500+ results", references))
    page.script("evaluate:job_total_pages", None)
    page.script("evaluate:job_ids", {"ids": ids, "scoped": True})
    page.script("evaluate:job_promoted_ids", ["102"])
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("search_jobs", "search_results"):
            result = await extractor.search_jobs("python", max_pages=1)
    page.assert_clean()
    return recorder.trace(
        {"method": "search_jobs", "arguments": {"keywords": "python", "max_pages": 1}},
        result,
    )


async def _job_search_upgrade_scenario() -> dict[str, Any]:
    recorder = TraceRecorder(
        "search_jobs__stopping_page_metadata_upgrade", _COMMON_ALLOWED
    )
    clock = FakeClock(recorder)
    page = _page(recorder)
    first = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Job",
            "heading": "",
        }
    ]
    second = [
        {
            "href": "https://www.linkedin.com/jobs/view/101/",
            "text": "Senior policy engineer with richer stopping-page metadata",
            "heading": "",
        }
    ]
    page.script(
        "evaluate:root_content",
        _root("First page", first),
        _root("Stopping page", second),
    )
    page.script("evaluate:job_total_pages", None)
    page.script(
        "evaluate:job_ids",
        {"ids": ["101"], "scoped": True},
        {"ids": ["101"], "scoped": True},
    )
    # Read for the first page only: the stopping page has no new ids to flag.
    page.script("evaluate:job_promoted_ids", [])
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("search_jobs", "search_results"):
            result = await extractor.search_jobs("python", max_pages=2)
    page.assert_clean()
    return recorder.trace(
        {"method": "search_jobs", "arguments": {"keywords": "python", "max_pages": 2}},
        result,
    )


async def _saved_jobs_scenario() -> dict[str, Any]:
    name = "get_saved_jobs__redirect_caps_and_upgrade"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append("https://www.linkedin.com/jobs-tracker/")
    first_references = [
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{100 + index}/",
                "text": f"Job {100 + index}",
                "heading": "",
            }
            for index in range(12)
        ],
        {
            "href": "https://www.linkedin.com/company/cap-boundary/",
            "text": "Cap boundary company",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{113 + index}/",
                "text": f"Job {113 + index}",
                "heading": "",
            }
            for index in range(7)
        ],
    ]
    second_references = [
        {
            "href": "https://www.linkedin.com/jobs/view/100/",
            "text": "Senior policy engineer with richer duplicate metadata",
            "heading": "",
        },
        *[
            {
                "href": f"https://www.linkedin.com/jobs/view/{112 + index}/",
                "text": f"Job {112 + index}",
                "heading": "",
            }
            for index in range(19)
        ],
    ]
    page.script(
        "evaluate:root_content",
        _root("Saved jobs page one", first_references),
        _root("Saved jobs page two", second_references),
    )
    page.script("evaluate:saved_job_total_pages", 2)
    page.script(
        "evaluate:job_ids",
        {"ids": [str(100 + index) for index in range(10)], "scoped": False},
        {"ids": [str(110 + index) for index in range(10)], "scoped": False},
    )
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_saved_jobs", "saved_jobs"):
            result = await extractor.get_saved_jobs(max_pages=2)
    page.assert_clean()
    references = result.get("references", {}).get("saved_jobs", [])
    return recorder.trace(
        {"method": "get_saved_jobs", "arguments": {"max_pages": 2}},
        _complete_mapping_result(result, reference_count=len(references)),
    )


_MESSAGE_PROFILE_URL = "https://www.linkedin.com/in/ada-lovelace/"
_MESSAGE_COMPOSE_URL = (
    "https://www.linkedin.com/messaging/compose/"
    "?recipient=ACoAA-policy&profileUrn=urn%3Ali%3Afsd_profile%3AACoAA-policy"
)


def _profile_target(
    status: str = "resolved", *, profile_urn: str = "ACoAA-policy"
) -> dict[str, Any]:
    if status == "resolved":
        compose_url = (
            _MESSAGE_COMPOSE_URL
            if profile_urn == "ACoAA-policy"
            else "https://www.linkedin.com/messaging/compose/?recipient=" + profile_urn
        )
        return {
            "status": "resolved",
            "pageUrl": _MESSAGE_PROFILE_URL,
            "displayName": "Ada Lovelace",
            "composeHrefs": [compose_url],
        }
    if status == "unavailable":
        return {"status": "unavailable", "pageUrl": _MESSAGE_PROFILE_URL}
    return {"status": "unresolved"}


def _script_profile_target(
    page: ScriptedPage,
    status: str = "resolved",
    *,
    profile_urn: str = "ACoAA-policy",
) -> None:
    page.script(
        "wait_for_function:profile_message_target_ready",
        None
        if status == "resolved"
        else PlaywrightTimeoutError("profile Message action did not resolve"),
    )
    page.script(
        "evaluate:profile_message_target",
        _profile_target(status, profile_urn=profile_urn),
    )


async def _single_capture_facade_scenario(method: str) -> dict[str, Any]:
    name = f"{method}__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    # A posting is only whole with its description heading; the other
    # facades accept any text.
    text = "About the job\nResult content" if method == "read_job" else "Result content"
    page = _page(recorder).script("evaluate:root_content", _root(text))
    extractor = _extractor(page)
    arguments: dict[str, Any]
    async with boundaries(recorder, clock):
        with recorder.context(method):
            if method == "get_company_employees":
                arguments = {"company_name": "analytical-engine", "keywords": "math"}
                result = await extractor.get_company_employees(**arguments)
            elif method == "read_job":
                arguments = {"job_id": "123"}
                result = await extractor.read_job(**arguments)
            elif method == "search_people":
                arguments = {"keywords": "analyst", "network": ["F"]}
                result = await extractor.search_people(**arguments)
            elif method == "search_companies":
                arguments = {"keywords": "engine"}
                result = await extractor.search_companies(**arguments)
            else:
                raise AssertionError(method)
    page.assert_clean()
    return recorder.trace(
        {"method": method, "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _single_capture_error_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("read_job__capture_error", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:root_content", RuntimeError("synthetic capture failure")
    )
    extractor = _extractor(page)
    arguments = {"job_id": "123"}
    async with boundaries(recorder, clock):
        with recorder.context("read_job"):
            result = await extractor.read_job(**arguments)
    page.assert_clean()
    return recorder.trace(
        {"method": "read_job", "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _description_missing_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("read_job__description_missing", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    # The header and company details rendered, the description panel did not.
    page = _page(recorder).script("evaluate:root_content", _root("Result content"))
    extractor = _extractor(page)
    arguments = {"job_id": "123"}
    async with boundaries(recorder, clock):
        with recorder.context("read_job"):
            result = await extractor.read_job(**arguments)
    page.assert_clean()
    return recorder.trace(
        {"method": "read_job", "arguments": arguments},
        _complete_mapping_result(result, section_names=list(result["sections"])),
    )


async def _job_apply_scenario() -> dict[str, Any]:
    recorder = TraceRecorder("get_job_apply_url__easy_apply", _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder).script(
        "evaluate:job_apply_signals",
        {"easy_apply": True, "external_link": None, "applied": False, "closed": False},
    )
    extractor = _extractor(page)
    arguments = {"job_id": "123"}
    async with boundaries(recorder, clock):
        with recorder.context("get_job_apply_url"):
            result = await extractor.get_job_apply_url(**arguments)
    page.assert_clean()
    return recorder.trace(
        {"method": "get_job_apply_url", "arguments": arguments}, result
    )


async def _get_my_profile_scenario() -> dict[str, Any]:
    name = "get_my_profile__baseline"
    recorder = TraceRecorder(name, _COMMON_ALLOWED)
    clock = FakeClock(recorder)
    page = _page(recorder)
    page.goto_landings.append("https://www.linkedin.com/in/ada-lovelace/")
    page.script("evaluate:root_content", _root("Own profile"))
    _script_profile_target(page, profile_urn="ACoAA-self")
    extractor = _extractor(page)
    async with boundaries(recorder, clock):
        with recorder.context("get_my_profile", "main_profile"):
            result = await extractor.get_my_profile()
    page.assert_clean()
    return recorder.trace(
        {"method": "get_my_profile", "arguments": {}},
        result,
    )


async def _facade_contract_trace() -> dict[str, Any]:
    global _TOOL_SCHEMAS

    methods = {}
    for name in TOOL_FACADE_METHODS | COMPATIBILITY_METHODS:
        member = getattr(LinkedInExtractor, name)
        methods[name] = {
            "signature": str(inspect.signature(member)),
            "coroutine": inspect.iscoroutinefunction(member),
        }
    if _TOOL_SCHEMAS is None:
        tools = await create_mcp_server().list_tools()
        _TOOL_SCHEMAS = {
            tool.name: {
                "input": tool.parameters,
                "output": tool.output_schema,
            }
            for tool in sorted(tools, key=lambda item: item.name)
        }
    return {
        "schema_version": 1,
        "scenario": "facade_contract",
        "call": {"method": "LinkedInExtractor", "arguments": {"constructor": "Page"}},
        "events": [],
        "result": {
            "tool_methods": sorted(TOOL_FACADE_METHODS),
            "compatibility_methods": sorted(COMPATIBILITY_METHODS),
            "methods": methods,
            "tool_schemas": _TOOL_SCHEMAS,
        },
    }


TOOL_FACADE_METHODS = {
    "extract_page",
    "get_company_employees",
    "get_job_apply_url",
    "get_my_profile",
    "get_saved_jobs",
    "read_company",
    "read_job",
    "read_person",
    "search_companies",
    "search_jobs",
    "search_people",
}
COMPATIBILITY_METHODS = {"get_page_text", "click_button_by_text"}


async def build_policy_traces() -> dict[str, dict[str, Any]]:
    traces = {
        "facade-contract.json": await _facade_contract_trace(),
        "generic-ordinary.json": await _generic_capture_scenario(
            "extract_page__ordinary", "https://www.linkedin.com/in/ada-lovelace/"
        ),
        "generic-activity.json": await _generic_capture_scenario(
            "extract_page__activity",
            "https://www.linkedin.com/in/ada-lovelace/recent-activity/all/",
        ),
        "generic-search.json": await _generic_capture_scenario(
            "extract_page__search",
            "https://www.linkedin.com/search/results/people/?keywords=ada",
        ),
        "generic-company-people.json": await _generic_capture_scenario(
            "extract_page__company_people",
            "https://www.linkedin.com/company/analytical-engine/people/",
        ),
        "person-sections.json": await _person_sections_scenario(),
        "company-sections.json": await _company_sections_scenario(),
        "job-search.json": await _job_search_scenario(),
        "job-search-route-alias.json": await _job_search_scenario(
            "/jobs/search-results/"
        ),
        "job-search-metadata-upgrade.json": await _job_search_upgrade_scenario(),
        "saved-jobs.json": await _saved_jobs_scenario(),
        "get-my-profile.json": await _get_my_profile_scenario(),
        "company-employees.json": await _single_capture_facade_scenario(
            "get_company_employees"
        ),
        "read-job.json": await _single_capture_facade_scenario("read_job"),
        "read-job-error.json": await _single_capture_error_scenario(),
        "read-job-description-missing.json": await _description_missing_scenario(),
        "job-apply-url.json": await _job_apply_scenario(),
        "search-people.json": await _single_capture_facade_scenario("search_people"),
        "search-companies.json": await _single_capture_facade_scenario(
            "search_companies"
        ),
    }
    return traces


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def policy_trace_diff(
    generated: dict[str, dict[str, Any]], trace_root: Path = TRACE_ROOT
) -> str:
    """Return one deterministic unified comparison against canonical traces."""

    generated_names = set(generated)
    fixture_names = {path.name for path in trace_root.glob("*.json")}
    chunks = [
        f"missing canonical trace: {trace_root / name}\n"
        for name in sorted(generated_names - fixture_names)
    ]
    chunks.extend(
        f"unexpected canonical trace: {trace_root / name}\n"
        for name in sorted(fixture_names - generated_names)
    )
    for name in sorted(generated_names & fixture_names):
        path = trace_root / name
        expected = path.read_text(encoding="utf-8")
        actual = canonical_json(generated[name])
        chunks.extend(
            unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"generated/{name}",
            )
        )
    return "".join(chunks)
