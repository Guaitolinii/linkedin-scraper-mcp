"""The server only reads LinkedIn: nothing it serves can write, send or click."""

from __future__ import annotations

import pytest

from linkedin_scraper_mcp.server import create_mcp_server
from linkedin_scraper_mcp.server_role import ServerRole

READ_ONLY_TOOLS = {
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

#: Every tool the upstream project serves that sends, connects, archives,
#: dismisses or opens a form, plus the reads of private inbox and feed data.
REMOVED_TOOLS = {
    "archive_conversation",
    "connect_with_person",
    "dismiss_job",
    "get_conversation",
    "get_easy_apply_form",
    "get_feed",
    "get_inbox",
    "get_sidebar_profiles",
    "reply_to_conversation",
    "search_conversations",
    "search_posts",
    "send_message",
}


async def _served(role: ServerRole) -> dict[str, object]:
    tools = await create_mcp_server(role=role).list_tools()
    return {tool.name: tool for tool in tools}


@pytest.mark.parametrize(
    "role", [role for role in ServerRole if role.drives_browser], ids=lambda r: r.name
)
async def test_the_server_serves_exactly_the_read_only_tools(role: ServerRole):
    served = await _served(role)

    assert set(served) == READ_ONLY_TOOLS


async def test_no_removed_tool_is_served():
    served = await _served(ServerRole.DIRECT)

    assert not REMOVED_TOOLS & set(served)


async def test_no_served_tool_is_declared_a_write():
    # A tool that writes has to say so to its client; none here does.
    # `close_session` only closes the local browser, so it is the one
    # destructive hint allowed, and it touches no LinkedIn data.
    served = await _served(ServerRole.DIRECT)

    for name, tool in served.items():
        annotations = getattr(tool, "annotations", None)
        if annotations is None or name == "close_session":
            continue
        assert annotations.read_only_hint is not False, name
        assert annotations.destructive_hint is not True, name


def test_the_removed_modules_are_gone():
    import importlib

    for module in (
        "linkedin_scraper_mcp.tools.messaging",
        "linkedin_scraper_mcp.tools.feed",
        "linkedin_scraper_mcp.tools.post",
        "linkedin_scraper_mcp.linkedin.message_sender",
        "linkedin_scraper_mcp.linkedin.connection_actions",
        "linkedin_scraper_mcp.linkedin.conversations",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)
