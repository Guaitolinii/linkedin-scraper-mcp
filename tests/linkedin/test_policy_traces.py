"""Canonical semantic trace checks for extractor policy."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import importlib.util
import json
import logging
import subprocess
import sys

import pytest

from linkedin_scraper_mcp.core.exceptions import AuthenticationError
from linkedin_scraper_mcp.linkedin.company import CompanyReader
from linkedin_scraper_mcp.linkedin.fields import COMPANY_SECTIONS, PERSON_SECTIONS
from linkedin_scraper_mcp.linkedin.navigation import PageNavigator
from linkedin_scraper_mcp.linkedin.person import PersonReader
from linkedin_scraper_mcp.linkedin.session import PageSession

from . import policy_scenarios
from .policy_scenarios import (
    TOOL_FACADE_METHODS,
    TRACE_ROOT,
    _complete_mapping_result,
    boundaries,
    build_policy_traces,
    canonical_json,
    policy_trace_diff,
)
from .support.policy_trace import FakeClock, ScriptedPage, TraceRecorder


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_policy_traces.py"
_CHECKER_SPEC = importlib.util.spec_from_file_location("check_policy_traces", CHECKER)
assert _CHECKER_SPEC is not None and _CHECKER_SPEC.loader is not None
_CHECKER_MODULE = importlib.util.module_from_spec(_CHECKER_SPEC)
_CHECKER_SPEC.loader.exec_module(_CHECKER_MODULE)


def _operation_positions(trace: dict[str, Any]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = {}
    for index, event in enumerate(trace["events"]):
        operation = event.get("operation", event["kind"])
        positions.setdefault(operation, []).append(index)
    return positions


async def test_generated_traces_match_every_canonical_fixture():
    generated = await build_policy_traces()

    assert policy_trace_diff(generated) == ""


def test_complete_result_rejects_derived_field_collisions():
    with pytest.raises(AssertionError, match="overlap raw result"):
        _complete_mapping_result({"section_names": []}, section_names=[])


async def test_navigation_boundaries_keep_full_auth_and_stabilization_order():
    trace = (await build_policy_traces())["job-search-route-alias.json"]
    positions = _operation_positions(trace)

    assert (
        positions["boundary.stabilize"][0]
        < positions["boundary.auth_quick"][0]
        < positions["boundary.auth"][0]
        < positions["root_content"][0]
    )
    assert trace["events"][positions["boundary.stabilize"][0]] == {
        "kind": "boundary.stabilize",
        "call": "search_jobs",
        "section": "search_results",
        "description": "goto https://www.linkedin.com/jobs/search/?keywords=python",
        "result": None,
    }
    assert trace["events"][positions["boundary.auth"][0]]["result"] is None


async def test_stabilization_trace_ignores_physical_logger_identity():
    recorder = TraceRecorder("stabilize-logger", {"boundary.stabilize"})
    clock = FakeClock(recorder)

    relocated_logger = logging.getLogger("linkedin_scraper_mcp.linkedin.navigation")

    async with boundaries(recorder, clock):
        await policy_scenarios.navigation_module.stabilize_navigation(
            "logical navigation", relocated_logger
        )

    assert recorder.events == [
        {
            "kind": "boundary.stabilize",
            "description": "logical navigation",
            "result": None,
        }
    ]


async def test_full_auth_boundary_propagates_a_detected_barrier():
    recorder = TraceRecorder("full-auth-barrier", {"boundary.auth"})
    clock = FakeClock(recorder)
    page = ScriptedPage(recorder)
    navigator = PageNavigator(PageSession(cast(Any, page)))

    async with boundaries(recorder, clock, auth_result="account picker"):
        with pytest.raises(AuthenticationError, match="interactive re-authentication"):
            await navigator._raise_if_auth_barrier(
                "https://www.linkedin.com/jobs/search/"
            )

    assert recorder.events == [{"kind": "boundary.auth", "result": "account picker"}]


async def test_trace_comparison_reports_a_unified_diff():
    generated = await build_policy_traces()
    generated["search-people.json"]["result"]["url"] = "mutated"

    difference = policy_trace_diff(generated)

    assert f"--- {TRACE_ROOT / 'search-people.json'}" in difference
    assert "+++ generated/search-people.json" in difference
    assert '-    "url":' in difference
    assert '+    "url": "mutated"' in difference


def test_canonical_fixtures_are_portable_deterministic_json():
    for path in sorted(TRACE_ROOT.glob("*.json")):
        raw = path.read_bytes()
        decoded = raw.decode("utf-8")
        value = json.loads(decoded)

        assert raw.endswith(b"\n")
        assert decoded == canonical_json(value)
        assert "/Users/" not in decoded
        assert '"timestamp"' not in decoded
        assert '"seq"' not in decoded


async def test_trace_set_exercises_every_tool_facing_facade_method():
    traces = await build_policy_traces()
    called = {
        trace["call"]["method"]
        for trace in traces.values()
        if trace["call"]["method"] in TOOL_FACADE_METHODS
    }

    assert called == TOOL_FACADE_METHODS


async def test_tool_schema_trace_keeps_people_boundary_coercion_and_inventory():
    schemas = (await build_policy_traces())["facade-contract.json"]["result"][
        "tool_schemas"
    ]

    assert set(schemas) == {
        "close_session",
        "get_company_employees",
        "get_company_posts",
        "get_company_profile",
        "get_job_apply_url",
        "get_job_details",
        "get_my_profile",
        "get_person_profile",
        "get_saved_jobs",
        "get_session_status",
        "search_companies",
        "search_jobs",
        "search_people",
    }
    network = schemas["search_people"]["input"]["properties"]["network"]
    assert network["anyOf"] == [
        {"items": {"type": "string"}, "type": "array"},
        {"type": "null"},
    ]
    assert 'comma-separated string ("F,S") is also' in network["description"]
    assert schemas["search_people"]["input"]["required"] == ["keywords"]


async def test_facade_results_keep_raw_values_and_optional_key_shape():
    traces = await build_policy_traces()
    company = traces["search-companies.json"]["result"]
    person = traces["person-sections.json"]["result"]
    assert company["sections"] == {"search_results": "Result content"}
    assert "references" not in company
    assert "section_errors" not in company
    assert person["references"]["experience"][0]["text"] == "Employer 0"


async def test_read_job_traces_keep_success_and_error_results_separate():
    traces = await build_policy_traces()
    successful_job = traces["read-job.json"]["result"]
    failed_job = traces["read-job-error.json"]["result"]
    headless_job = traces["read-job-description-missing.json"]["result"]

    assert successful_job["sections"] == {
        "job_posting": "About the job\nResult content"
    }
    assert successful_job["section_names"] == ["job_posting"]
    assert "section_errors" not in successful_job
    assert failed_job["sections"] == {}
    assert failed_job["section_names"] == []
    assert failed_job["section_errors"] == {
        "job_posting": {
            "context": "extract_page",
            "error_message": "synthetic capture failure",
            "error_type": "RuntimeError",
        }
    }
    # Kept, not dropped: the header and company details are still the posting.
    assert headless_job["sections"] == {"job_posting": "Result content"}
    assert (
        headless_job["section_errors"]["job_posting"]["error_type"]
        == "description_missing"
    )


async def test_facade_trace_detects_section_text_corruption():
    original = CompanyReader.search_companies

    @wraps(original)
    async def corrupt_sections(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(self, *args, **kwargs)
        result["sections"] = {name: "corrupted text" for name in result["sections"]}
        return result

    with patch.object(CompanyReader, "search_companies", corrupt_sections):
        mutated = await build_policy_traces()

    assert "corrupted text" in policy_trace_diff(mutated)


async def test_facade_trace_detects_lost_references():
    original = PersonReader.read_person
    removed: list[dict[str, Any]] = []

    @wraps(original)
    async def drop_references(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(self, *args, **kwargs)
        references = result.pop("references", None)
        if references:
            removed.append(references)
        return result

    with patch.object(PersonReader, "read_person", drop_references):
        mutated = await build_policy_traces()

    assert removed
    assert '-    "references": {' in policy_trace_diff(mutated)


async def test_facade_trace_detects_optional_key_drift():
    original = CompanyReader.search_companies

    @wraps(original)
    async def add_optional_key(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original(self, *args, **kwargs)
        result["section_errors"] = {}
        return result

    with patch.object(CompanyReader, "search_companies", add_optional_key):
        mutated = await build_policy_traces()

    assert '+    "section_errors": {}' in policy_trace_diff(mutated)


async def test_section_navigation_and_callbacks_remain_one_to_one():
    traces = await build_policy_traces()
    person = traces["person-sections.json"]
    company = traces["company-sections.json"]

    assert person["result"]["section_names"] == list(PERSON_SECTIONS)
    assert company["result"]["section_names"] == list(COMPANY_SECTIONS)
    assert sum(e["kind"] == "navigate" for e in person["events"]) == len(
        PERSON_SECTIONS
    )
    assert sum(e["kind"] == "navigate" for e in company["events"]) == len(
        COMPANY_SECTIONS
    )
    assert sum(e["kind"] == "callback.start" for e in person["events"]) == 1
    assert sum(e["kind"] == "callback.progress" for e in person["events"]) == len(
        PERSON_SECTIONS
    )
    assert sum(e["kind"] == "callback.complete" for e in person["events"]) == 1


async def test_job_capture_precedes_validation_dependent_reads():
    trace = (await build_policy_traces())["job-search.json"]
    positions = _operation_positions(trace)

    root_read = positions["root_content"][0]
    post_capture_identity = positions["document_origin"][1]
    total_pages = positions["job_total_pages"][0]
    job_ids = positions["job_ids"][0]

    assert root_read < post_capture_identity < total_pages < job_ids
    expected_ids = [str(101 + index) for index in range(16)]
    references = trace["result"]["references"]["search_results"]
    assert trace["result"]["job_ids"] == expected_ids
    assert [reference["url"] for reference in references] == [
        f"/jobs/view/{job_id}/" for job_id in expected_ids
    ]
    assert references[0]["text"] == "Senior policy engineer"


async def test_job_reference_caps_fallbacks_and_stopping_page_upgrade():
    traces = await build_policy_traces()
    alias = traces["job-search-route-alias.json"]["result"]
    alias_references = alias["references"]["search_results"]
    upgraded = traces["job-search-metadata-upgrade.json"]["result"]

    assert alias["job_ids"] == ["101", "102"]
    assert len(alias_references) == 15
    assert sum(ref["kind"] == "job" for ref in alias_references) == 2
    assert sum(ref["kind"] != "job" for ref in alias_references) == 13
    assert upgraded["job_ids"] == ["101"]
    assert upgraded["references"]["search_results"] == [
        {
            "kind": "job",
            "url": "/jobs/view/101/",
            "text": "Senior policy engineer with richer stopping-page metadata",
            "context": "job result",
        }
    ]


async def test_saved_jobs_keep_their_caps_and_ordering():
    traces = await build_policy_traces()
    saved = traces["saved-jobs.json"]

    assert len(saved["result"]["job_ids"]) == 20
    assert saved["result"]["reference_count"] == 15
    assert saved["result"]["references"]["saved_jobs"][0]["text"] == (
        "Senior policy engineer with richer duplicate metadata"
    )


def test_trace_checker_generates_only_outside_canonical_fixture_tree(tmp_path):
    output = tmp_path / "candidate-traces"
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--output", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert {path.name for path in output.glob("*.json")} == {
        path.name for path in TRACE_ROOT.glob("*.json")
    }

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--output", str(TRACE_ROOT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert (
        "refusing to write generated output inside canonical fixture" in result.stderr
    )
