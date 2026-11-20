import asyncio
import logging
from typing import Any, Callable, Coroutine, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from linkedin_scraper_mcp.callbacks import MCPContextProgressCallback
from linkedin_scraper_mcp.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
)
from linkedin_scraper_mcp.linkedin.contracts import ExtractedSection


async def get_tool_fn(
    mcp: FastMCP, name: str
) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """Extract tool function from FastMCP by name using public API."""
    tool = await mcp.get_tool(name)
    if tool is None:
        raise ValueError(f"Tool '{name}' not found")
    return cast(FunctionTool, tool).fn


def _make_mock_extractor(result: dict) -> MagicMock:
    """Create a mock LinkedInExtractor that returns the given result."""
    mock = MagicMock()
    mock.read_person = AsyncMock(return_value=result)
    mock.read_company = AsyncMock(return_value=result)
    mock.read_job = AsyncMock(return_value=result)
    mock.search_jobs = AsyncMock(return_value=result)
    mock.get_saved_jobs = AsyncMock(return_value=result)
    mock.get_job_apply_url = AsyncMock(return_value=result)
    mock.search_people = AsyncMock(return_value=result)
    mock.get_my_profile = AsyncMock(return_value=result)
    mock.search_companies = AsyncMock(return_value=result)
    mock.get_company_employees = AsyncMock(return_value=result)
    mock.extract_page = AsyncMock(
        return_value=ExtractedSection(text="some text", references=[])
    )
    return mock


_TOOL_MODULES = ("person", "company", "job")


@pytest.fixture
def serve_extractor(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], AsyncMock]:
    """Answer every tool's readiness call with the given extractor.

    Patched in each tool module, which is where the tools look it up, so a tool
    body runs unchanged from the readiness call onwards.
    """

    def serve(extractor: Any) -> AsyncMock:
        ready = AsyncMock(return_value=extractor)
        for module in _TOOL_MODULES:
            monkeypatch.setattr(
                f"linkedin_scraper_mcp.tools.{module}.get_ready_extractor", ready
            )
        return ready

    return serve


@pytest.mark.parametrize(
    ("module_name", "tool_name", "arguments", "error_match"),
    [
        (
            "person",
            "get_person_profile",
            {"linkedin_username": "/feed/"},
            "not a personal profile",
        ),
        (
            "person",
            "search_people",
            {"keywords": "engineer", "current_company": "SAP"},
            "numeric LinkedIn company URN id",
        ),
        (
            "person",
            "search_people",
            {"keywords": "engineer", "location": "New York"},
            "numeric LinkedIn geo URN id",
        ),
        (
            "person",
            "search_people",
            {"keywords": "", "connection_of": "williamhgates"},
            "LinkedIn profile id",
        ),
        (
            "company",
            "get_company_profile",
            {"company_name": "/feed/"},
            "not a company page",
        ),
        (
            "company",
            "get_company_posts",
            {"company_name": "/feed/"},
            "not a company page",
        ),
        (
            "company",
            "get_company_employees",
            {"company_name": "/feed/"},
            "not a company page",
        ),
        (
            "job",
            "get_job_details",
            {"job_id": "/feed/"},
            "job_id is not a LinkedIn id",
        ),
        (
            "job",
            "get_job_apply_url",
            {"job_id": "/feed/"},
            "job_id is not a LinkedIn id",
        ),
    ],
)
async def test_invalid_reference_is_rejected_before_extractor(
    module_name, tool_name, arguments, error_match
):
    from fastmcp.exceptions import ToolError

    from linkedin_scraper_mcp.tools.company import register_company_tools
    from linkedin_scraper_mcp.tools.job import register_job_tools
    from linkedin_scraper_mcp.tools.person import register_person_tools

    register_by_module = {
        "company": register_company_tools,
        "job": register_job_tools,
        "person": register_person_tools,
    }
    mcp = FastMCP("test")
    register_by_module[module_name](mcp)
    ready = AsyncMock(side_effect=AssertionError("get_ready_extractor was called"))

    with patch(f"linkedin_scraper_mcp.tools.{module_name}.get_ready_extractor", ready):
        with pytest.raises(ToolError, match=error_match):
            await mcp.call_tool(tool_name, arguments)

    ready.assert_not_awaited()


#: One call per registered tool that passes boundary validation, so the tool
#: reaches its readiness call. `test_the_valid_calls_name_every_tool` keeps it
#: complete, which is what makes the tests below cover a tool added later.
_A_VALID_CALL = [
    ("person", "get_person_profile", {"linkedin_username": "alice"}),
    ("person", "search_people", {"keywords": "engineer"}),
    ("person", "get_my_profile", {}),
    ("company", "get_company_profile", {"company_name": "anthropic"}),
    ("company", "get_company_posts", {"company_name": "anthropic"}),
    ("company", "search_companies", {"keywords": "fintech"}),
    ("company", "get_company_employees", {"company_name": "anthropic"}),
    ("job", "get_job_details", {"job_id": "4252026496"}),
    ("job", "get_job_apply_url", {"job_id": "4252026496"}),
    ("job", "search_jobs", {"keywords": "python"}),
    ("job", "get_saved_jobs", {}),
]


def _register(mcp: FastMCP, module_name: str) -> None:
    import importlib

    module = importlib.import_module(f"linkedin_scraper_mcp.tools.{module_name}")
    getattr(module, f"register_{module_name}_tools")(mcp)


@pytest.mark.parametrize(("module_name", "tool_name", "arguments"), _A_VALID_CALL)
async def test_valid_input_reaches_the_patched_readiness_call(
    module_name, tool_name, arguments
):
    # The counterpart of the test above. Without it, a patch on an attribute
    # the tool no longer reads would leave "never awaited" true for invalid
    # input and prove nothing about validation order.
    import importlib

    module = importlib.import_module(f"linkedin_scraper_mcp.tools.{module_name}")
    mcp = FastMCP("test")
    getattr(module, f"register_{module_name}_tools")(mcp)
    ready = AsyncMock(
        return_value=_make_mock_extractor(
            {"url": "https://www.linkedin.com/", "sections": {}}
        )
    )

    with patch(f"linkedin_scraper_mcp.tools.{module_name}.get_ready_extractor", ready):
        await mcp.call_tool(tool_name, arguments)

    ready.assert_awaited_once()
    assert ready.await_args is not None
    assert ready.await_args.kwargs == {"tool_name": tool_name}


async def test_the_valid_calls_name_every_tool():
    mcp = FastMCP("test")
    for module_name in _TOOL_MODULES:
        _register(mcp, module_name)

    registered = {tool.name for tool in await mcp.list_tools()}

    assert registered == {tool_name for _, tool_name, _ in _A_VALID_CALL}


@pytest.mark.parametrize(("module_name", "tool_name", "arguments"), _A_VALID_CALL)
async def test_a_session_that_dies_during_a_call_asks_for_a_login(
    module_name, tool_name, arguments, caplog
):
    """Every tool starts a login, and does not claim the call may run again.

    The readiness call is only where the failure is injected, being the one call
    every tool makes; the real readiness call answers its own auth failures.
    """
    from fastmcp import Context
    from fastmcp.exceptions import ToolError

    from linkedin_scraper_mcp.core.exceptions import AuthenticationError
    from linkedin_scraper_mcp.exceptions import AuthenticationStartedError

    mcp = FastMCP("test", mask_error_details=True)
    _register(mcp, module_name)
    signed_out = AuthenticationError("LinkedIn signed this session out")

    with (
        patch(
            f"linkedin_scraper_mcp.tools.{module_name}.get_ready_extractor",
            AsyncMock(side_effect=signed_out),
        ),
        patch(
            "linkedin_scraper_mcp.dependencies.handle_auth_error",
            AsyncMock(side_effect=AuthenticationStartedError("A login window opened")),
        ) as relogin,
        caplog.at_level(logging.INFO, logger="linkedin_scraper_mcp.error_handler"),
        pytest.raises(ToolError, match="A login window opened"),
    ):
        await mcp.call_tool(tool_name, arguments)

    relogin.assert_awaited_once()
    assert relogin.await_args is not None
    (failure, ctx), options = relogin.await_args
    assert failure is signed_out
    assert isinstance(ctx, Context)
    assert options.get("nothing_ran_yet", False) is False
    assert f"Authentication started in {tool_name}:" in caplog.text


@pytest.mark.parametrize(("module_name", "tool_name", "arguments"), _A_VALID_CALL)
async def test_a_known_failure_reaches_a_masked_client_shaped(
    module_name, tool_name, arguments, caplog
):
    from fastmcp.exceptions import ToolError

    from linkedin_scraper_mcp.core.exceptions import RateLimitError

    mcp = FastMCP("test", mask_error_details=True)
    _register(mcp, module_name)

    with (
        patch(
            f"linkedin_scraper_mcp.tools.{module_name}.get_ready_extractor",
            AsyncMock(side_effect=RateLimitError("throttled", suggested_wait_time=60)),
        ),
        patch(
            "linkedin_scraper_mcp.dependencies.handle_auth_error",
            new_callable=AsyncMock,
        ) as relogin,
        caplog.at_level(logging.WARNING, logger="linkedin_scraper_mcp.error_handler"),
        pytest.raises(ToolError, match="Rate limit detected. Wait 60 seconds"),
    ):
        await mcp.call_tool(tool_name, arguments)

    relogin.assert_not_awaited()
    assert f"Rate limit in {tool_name}:" in caplog.text


@pytest.mark.parametrize(("module_name", "tool_name", "arguments"), _A_VALID_CALL)
async def test_a_quiescent_owner_lets_every_tool_be_replayed_after_sign_in(
    module_name, tool_name, arguments
):
    """The readiness refusal crosses the tool with its replay marker intact.

    Readiness runs for real up to the owner's latch, so the refusal says that
    nothing ran yet from where it was raised, and the marker has to carry that
    through the tool's own failure handling to the client.
    """
    from fastmcp import Client

    from linkedin_scraper_mcp.bootstrap import go_auth_quiescent
    from linkedin_scraper_mcp.daemon.auth_repair import (
        MARKER_KEY,
        OwnerAuthSignalMiddleware,
    )
    from linkedin_scraper_mcp.server_role import ServerRole, set_process_role

    set_process_role(ServerRole.OWNER)
    go_auth_quiescent("broken-generation")
    mcp = FastMCP("owner", mask_error_details=True)
    _register(mcp, module_name)
    mcp.add_middleware(OwnerAuthSignalMiddleware())

    with patch(
        "linkedin_scraper_mcp.dependencies.get_or_create_browser",
        AsyncMock(side_effect=AssertionError("a quiescent owner opened a browser")),
    ):
        async with Client(mcp) as client:
            result = await client.call_tool(tool_name, arguments, raise_on_error=False)

    assert result.is_error
    marker = (result.meta or {})[MARKER_KEY]
    assert marker["replayable"] is True
    assert marker["generation"] == "broken-generation"


@pytest.mark.parametrize(("module_name", "tool_name", "arguments"), _A_VALID_CALL)
async def test_no_tool_hands_a_proxy_credential_to_the_client(
    module_name, tool_name, arguments, monkeypatch
):
    """An error that is already shaped is still checked for the credentials."""
    from fastmcp.exceptions import ToolError

    from linkedin_scraper_mcp.config.schema import AppConfig

    config = AppConfig()
    config.browser.proxy_server = "http://gate.example:7000"
    config.browser.proxy_username = "acctzone9"
    config.browser.proxy_password = "s3cr3t"
    monkeypatch.setattr("linkedin_scraper_mcp.config.get_config", lambda: config)
    mcp = FastMCP("test", mask_error_details=True)
    _register(mcp, module_name)
    shaped = ToolError("Proxy refused http://acctzone9:s3cr3t@gate.example:7000")

    with (
        patch(
            f"linkedin_scraper_mcp.tools.{module_name}.get_ready_extractor",
            AsyncMock(side_effect=shaped),
        ),
        pytest.raises(ToolError) as excinfo,
    ):
        await mcp.call_tool(tool_name, arguments)

    assert "gate.example:7000" in str(excinfo.value)
    assert "s3cr3t" not in str(excinfo.value)
    assert "acctzone9" not in str(excinfo.value)


async def test_a_search_filter_refusal_does_not_echo_a_proxy_credential(
    monkeypatch,
):
    """The refusal quotes the filter back, and is answered before readiness."""
    from fastmcp.exceptions import ToolError

    from linkedin_scraper_mcp.config.schema import AppConfig

    config = AppConfig()
    config.browser.proxy_server = "http://gate.example:7000"
    config.browser.proxy_password = "s3cr3t"
    monkeypatch.setattr("linkedin_scraper_mcp.config.get_config", lambda: config)
    mcp = FastMCP("test", mask_error_details=True)
    _register(mcp, "person")

    with pytest.raises(ToolError, match="numeric LinkedIn company URN id") as excinfo:
        await mcp.call_tool(
            "search_people", {"keywords": "engineer", "current_company": "s3cr3t"}
        )

    assert "s3cr3t" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("module_name", "tool_name", "method", "arguments"),
    [
        ("person", "get_person_profile", "read_person", {"linkedin_username": "a"}),
        ("person", "get_my_profile", "get_my_profile", {}),
    ],
)
async def test_a_landing_off_linkedin_reaches_a_masked_client_by_name(
    module_name, tool_name, method, arguments
):
    """The one useful fact, where the browser landed, survives the masking.

    And the session stays: this is the network, so the recovery for an expired
    session, which retires the profile and opens a login, must not run.
    """
    import importlib

    from fastmcp.exceptions import ToolError

    from linkedin_scraper_mcp.core.destination import raise_if_off_linkedin
    from linkedin_scraper_mcp.core.exceptions import OffLinkedInLandingError

    try:
        raise_if_off_linkedin("https://portal.invalid/interstitial?token=s3cret")
    except OffLinkedInLandingError as landed:
        refusal = landed

    module = importlib.import_module(f"linkedin_scraper_mcp.tools.{module_name}")
    mcp = FastMCP("test", mask_error_details=True)
    getattr(module, f"register_{module_name}_tools")(mcp)
    extractor = _make_mock_extractor({})
    setattr(extractor, method, AsyncMock(side_effect=refusal))

    with (
        patch(
            f"linkedin_scraper_mcp.tools.{module_name}.get_ready_extractor",
            AsyncMock(return_value=extractor),
        ),
        patch(
            "linkedin_scraper_mcp.dependencies.handle_auth_error",
            new_callable=AsyncMock,
        ) as relogin,
        pytest.raises(ToolError) as excinfo,
    ):
        await mcp.call_tool(tool_name, arguments)

    message = str(excinfo.value)
    assert "https://portal.invalid" in message
    assert "instead of LinkedIn" in message
    assert "s3cret" not in message
    relogin.assert_not_awaited()


class TestPersonTool:
    async def test_get_person_profile_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe\nSoftware Engineer"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn(
            "https://de.linkedin.com/in/test-user/",
            mock_context,
        )
        assert result["url"] == "https://www.linkedin.com/in/test-user/"
        assert "main_profile" in result["sections"]
        assert "pages_visited" not in result
        assert "sections_requested" not in result
        assert mock_extractor.read_person.await_args.args[0] == "test-user"

    async def test_get_person_profile_with_sections(
        self, mock_context, serve_extractor
    ):
        """Verify sections parameter is passed through."""
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {
                "main_profile": "John Doe",
                "experience": "Work history",
                "contact_info": "Email: test@test.com",
            },
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn(
            "test-user",
            mock_context,
            sections="experience,contact_info",
        )
        assert "main_profile" in result["sections"]
        assert "experience" in result["sections"]
        assert "contact_info" in result["sections"]
        # Verify read_person was called exactly once with a set[str]
        mock_extractor.read_person.assert_awaited_once()
        call_args = mock_extractor.read_person.call_args
        assert isinstance(call_args[0][1], set)
        assert "experience" in call_args[0][1]
        assert "contact_info" in call_args[0][1]

    async def test_get_person_profile_passes_callbacks(
        self, mock_context, serve_extractor
    ):
        """Verify tool wires MCPContextProgressCallback to the extractor."""
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        await tool_fn("test-user", mock_context)

        call_kwargs = mock_extractor.read_person.call_args.kwargs
        assert "callbacks" in call_kwargs
        assert isinstance(call_kwargs["callbacks"], MCPContextProgressCallback)

    async def test_get_person_profile_passes_max_scrolls(
        self, mock_context, serve_extractor
    ):
        """Verify max_scrolls parameter is forwarded to read_person."""
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        await tool_fn(
            "test-user",
            mock_context,
            max_scrolls=15,
        )

        call_kwargs = mock_extractor.read_person.call_args.kwargs
        assert call_kwargs["max_scrolls"] == 15

    async def test_get_person_profile_rejects_invalid_max_scrolls(self, mock_context):
        """Verify max_scrolls=0 is rejected by Field(ge=1) validation."""
        # FastMCP wraps the pydantic error raised by Field() constraints in
        # its own ValidationError, which does not subclass pydantic's.
        from fastmcp.exceptions import ValidationError

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        with pytest.raises(ValidationError, match="max_scrolls"):
            await mcp.call_tool(
                "get_person_profile",
                {"linkedin_username": "test-user", "max_scrolls": 0},
            )

    async def test_get_person_profile_unknown_section(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": "https://www.linkedin.com/in/test-user/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        result = await tool_fn(
            "test-user",
            mock_context,
            sections="bogus_section",
        )
        assert result["unknown_sections"] == ["bogus_section"]

    async def test_get_person_profile_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_scraper_mcp.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.read_person = AsyncMock(side_effect=SessionExpiredError())

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_person_profile")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn("test-user", mock_context)

    async def test_get_person_profile_auth_error(self, monkeypatch):
        """Auth failures in the DI layer trigger auto-relogin and report the login browser."""
        from fastmcp.exceptions import ToolError

        from linkedin_scraper_mcp.core.exceptions import AuthenticationError
        from linkedin_scraper_mcp.exceptions import AuthenticationStartedError

        # Live on purpose: a bare mock's ``is_closed()`` is truthy, which reads
        # as a closed page and sends the call down the shutdown path instead of
        # the auth failure this test is about.
        mock_browser = MagicMock()
        mock_browser.page.is_closed.return_value = False
        mock_browser.page_crashed = False
        mock_browser.context.browser.is_connected.return_value = True
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.ensure_tool_ready_or_raise",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.get_or_create_browser",
            AsyncMock(return_value=mock_browser),
        )
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.ensure_authenticated",
            AsyncMock(side_effect=AuthenticationError("Session expired or invalid.")),
        )
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.get_runtime_policy",
            lambda: "managed",
        )
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.close_browser",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.invalidate_auth_and_trigger_relogin",
            AsyncMock(
                side_effect=AuthenticationStartedError(
                    "Session expired. A login browser window has been opened."
                )
            ),
        )

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        with pytest.raises(ToolError, match="Session expired"):
            await mcp.call_tool("get_person_profile", {"linkedin_username": "test"})

    async def test_search_people(self, mock_context, serve_extractor):
        expected = {
            "url": (
                "https://www.linkedin.com/search/results/people/"
                "?keywords=AI+engineer&geoUrn=%5B%22103644278%22%5D"
            ),
            "sections": {"search_results": "Jane Doe\nAI Engineer at Acme\nNew York"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_people")
        result = await tool_fn("AI engineer", mock_context, location="103644278")
        assert "search_results" in result["sections"]
        assert "pages_visited" not in result
        mock_extractor.search_people.assert_awaited_once_with(
            "AI engineer",
            "103644278",
            network=None,
            current_company=None,
            connection_of=None,
        )

    async def test_search_people_with_network_and_company_filters(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": (
                "https://www.linkedin.com/search/results/people/"
                "?keywords=engineer&network=%5B%22F%22%5D"
                "&currentCompany=%5B%221115%22%5D"
            ),
            "sections": {
                "search_results": "Jennifer Bonuso\nPresident Americas at SAP"
            },
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_people")
        result = await tool_fn(
            "engineer",
            mock_context,
            network=["F"],
            current_company="1115",
        )
        assert "search_results" in result["sections"]
        mock_extractor.search_people.assert_awaited_once_with(
            "engineer",
            None,
            network=["F"],
            current_company="1115",
            connection_of=None,
        )

    async def test_search_people_with_connection_of_filter(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": (
                "https://www.linkedin.com/search/results/people/"
                "?keywords=&network=%5B%22F%22%5D"
                "&connectionOf=%5B%22ACoAADdZSNYBiaacxEw6je-wVIKjGkKp-it0gD8%22%5D"
            ),
            "sections": {"search_results": "Jane Doe\n1st"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_people")
        await tool_fn(
            "",
            mock_context,
            network=["F"],
            connection_of="ACoAADdZSNYBiaacxEw6je-wVIKjGkKp-it0gD8",
        )
        mock_extractor.search_people.assert_awaited_once_with(
            "",
            None,
            network=["F"],
            current_company=None,
            connection_of="ACoAADdZSNYBiaacxEw6je-wVIKjGkKp-it0gD8",
        )

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("F", ["F"]),
            ('["F"]', ["F"]),
            ('["F", "S"]', ["F", "S"]),
            ("F,S", ["F", "S"]),
            (" F , S ", ["F", "S"]),
            ("", []),
            (["F"], ["F"]),
            (None, None),
        ],
    )
    def test_coerce_str_list_repairs_stringified_arrays(self, raw, expected):
        """A client that flattens the array must still produce a list.

        Lists and None pass through untouched, so a well-behaved client is
        unaffected.
        """
        from linkedin_scraper_mcp.tools.person import _coerce_str_list

        assert _coerce_str_list(raw) == expected

    async def test_search_people_accepts_stringified_network(self, monkeypatch):
        """Regression for #739.

        The published schema for ``network`` is ``anyOf: [array, null]``, but
        clients that collapse that union send a bare string. Going through
        ``call_tool`` exercises the pydantic validation the direct-``fn`` tests
        skip, which is where the original failure lived.
        """
        import linkedin_scraper_mcp.tools.person as person_module
        from linkedin_scraper_mcp.tools.person import register_person_tools

        expected = {
            "url": (
                "https://www.linkedin.com/search/results/people/"
                "?keywords=engineer&network=%5B%22F%22%5D"
            ),
            "sections": {"search_results": "Jane Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        async def _fake_get_ready_extractor(ctx, tool_name):
            return mock_extractor

        monkeypatch.setattr(
            person_module, "get_ready_extractor", _fake_get_ready_extractor
        )

        mcp = FastMCP("test")
        register_person_tools(mcp)

        await mcp.call_tool("search_people", {"keywords": "engineer", "network": "F"})

        mock_extractor.search_people.assert_awaited_once_with(
            "engineer",
            None,
            network=["F"],
            current_company=None,
            connection_of=None,
        )

    async def test_search_people_validation_error_surfaced_as_tool_error(
        self, mock_context, serve_extractor
    ):
        """A FilterValidationError raised by the extractor should surface to
        the MCP client as a ToolError carrying the same message, rather than
        being collapsed to the generic "Error calling tool" mask."""
        from fastmcp.exceptions import ToolError

        from linkedin_scraper_mcp.linkedin.contracts import FilterValidationError
        from linkedin_scraper_mcp.tools.person import register_person_tools

        mock_extractor = MagicMock()
        mock_extractor.search_people = AsyncMock(
            side_effect=FilterValidationError("must be a numeric URN")
        )

        mcp = FastMCP("test")
        register_person_tools(mcp)
        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_people")

        with pytest.raises(ToolError, match="must be a numeric URN"):
            await tool_fn(
                "engineer",
                mock_context,
                current_company="1115",
            )

    @pytest.mark.parametrize("found_by", ["startup", "read"])
    async def test_a_restricted_account_is_reported_not_relogged(
        self, monkeypatch, serve_extractor, found_by: str
    ):
        """The session stays in place and no login window opens.

        Found either by the startup feed check or by a navigation mid-read;
        both reach the tool body, which routes only auth failures to re-login.
        """
        from fastmcp.exceptions import ToolError

        from linkedin_scraper_mcp.core.exceptions import AccountRestrictedError
        from linkedin_scraper_mcp.exceptions import AuthenticationStartedError

        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.ensure_tool_ready_or_raise",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.get_or_create_browser",
            AsyncMock(side_effect=AccountRestrictedError()),
        )
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.get_runtime_policy",
            lambda: "managed",
        )
        close_browser = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.close_browser", close_browser
        )
        relogin = AsyncMock(
            side_effect=AuthenticationStartedError(
                "Session expired. A login browser window has been opened."
            )
        )
        monkeypatch.setattr(
            "linkedin_scraper_mcp.dependencies.invalidate_auth_and_trigger_relogin",
            relogin,
        )
        if found_by == "read":
            extractor = MagicMock()
            extractor.read_person = AsyncMock(side_effect=AccountRestrictedError())
            serve_extractor(extractor)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        with pytest.raises(ToolError) as exc_info:
            await mcp.call_tool(
                "get_person_profile",
                {"linkedin_username": "test-user"},
            )

        # Whole, and nothing appended: no issue template for LinkedIn's decision.
        assert str(exc_info.value) == str(AccountRestrictedError())
        relogin.assert_not_awaited()
        close_browser.assert_not_awaited()


class TestCompanyTools:
    async def test_get_company_profile(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "TestCorp\nWe build things"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        result = await tool_fn(
            "https://uk.linkedin.com/company/testcorp/",
            mock_context,
        )
        assert "about" in result["sections"]
        assert "pages_visited" not in result
        assert (
            mock_extractor.read_company.await_args.args[0]
            == "https://uk.linkedin.com/company/testcorp/"
        )

    @pytest.mark.parametrize(
        "tool_name", ["get_company_profile", "get_company_employees"]
    )
    @pytest.mark.parametrize("slug", ["linkedin.com", "lnkd.in"])
    async def test_company_collision_slug_reaches_reader(
        self, mock_context, serve_extractor, tool_name, slug
    ):
        from linkedin_scraper_mcp.linkedin.company import CompanyReader
        from linkedin_scraper_mcp.tools.company import register_company_tools

        capture = MagicMock()
        capture.capture = AsyncMock(
            return_value=ExtractedSection(text="company text", references=[])
        )
        reader = CompanyReader(MagicMock(), capture)
        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(reader)
        tool_fn = await get_tool_fn(mcp, tool_name)
        result = await tool_fn(f"/company/{slug}/", mock_context)

        suffix = "/" if tool_name == "get_company_profile" else "/people/"
        url = f"https://www.linkedin.com/company/{slug}{suffix}"
        assert result["url"] == url
        assert result["sections"]
        capture.capture.assert_awaited_once()
        assert capture.capture.await_args is not None
        capture_suffix = "/about/" if tool_name == "get_company_profile" else suffix
        assert capture.capture.await_args.args[0] == (
            f"https://www.linkedin.com/company/{slug}{capture_suffix}"
        )

    async def test_get_company_posts_normalizes_a_pasted_link(
        self, mock_context, serve_extractor
    ):
        """get_company_posts builds its URL in the tool, not in the extractor.

        That makes it the one wiring point the extractor tests cannot reach, and
        the only place a pasted company link would still become
        /company/https://de.linkedin.com/company/testcorp/posts/.
        """
        mock_extractor = _make_mock_extractor({})

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn(
            "https://de.linkedin.com/company/testcorp/",
            mock_context,
        )
        assert result["url"] == "https://www.linkedin.com/company/testcorp/posts/"
        assert (
            mock_extractor.extract_page.call_args.args[0]
            == "https://www.linkedin.com/company/testcorp/posts/"
        )

    async def test_get_company_posts_refuses_a_traversal_value(
        self, mock_context, serve_extractor
    ):
        mock_extractor = _make_mock_extractor({})

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        with pytest.raises(Exception):
            await tool_fn("../../feed", mock_context)
        mock_extractor.extract_page.assert_not_called()

    async def test_get_company_profile_passes_callbacks(
        self, mock_context, serve_extractor
    ):
        """Verify tool wires MCPContextProgressCallback to the extractor."""
        expected = {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "TestCorp\nWe build things"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        await tool_fn("testcorp", mock_context)

        call_kwargs = mock_extractor.read_company.call_args.kwargs
        assert "callbacks" in call_kwargs
        assert isinstance(call_kwargs["callbacks"], MCPContextProgressCallback)

    async def test_get_company_profile_unknown_section(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": "https://www.linkedin.com/company/testcorp/",
            "sections": {"about": "TestCorp\nWe build things"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_profile")
        result = await tool_fn("testcorp", mock_context, sections="bogus")
        assert result["unknown_sections"] == ["bogus"]

    async def test_get_company_posts(self, mock_context, serve_extractor):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(text="Post 1\nPost 2", references=[])
        )

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context)
        assert "posts" in result["sections"]
        assert result["sections"]["posts"] == "Post 1\nPost 2"
        assert "pages_visited" not in result
        assert "sections_requested" not in result

    async def test_get_company_posts_passes_max_scrolls(
        self, mock_context, serve_extractor
    ):
        """Verify max_scrolls parameter is forwarded to extract_page."""
        mock_extractor = _make_mock_extractor({})

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        await tool_fn("testcorp", mock_context, max_scrolls=25)

        assert mock_extractor.extract_page.call_args.kwargs["max_scrolls"] == 25

    async def test_get_company_posts_omits_rate_limited_sentinel(
        self, mock_context, serve_extractor
    ):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        )

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context)
        assert result["sections"] == {}
        assert result["section_errors"]["posts"]["error_type"] == "rate_limit"

    async def test_get_company_posts_returns_section_errors(
        self, mock_context, serve_extractor
    ):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(
                text="",
                references=[],
                error={"issue_template_path": "/tmp/company-posts-issue.md"},
            )
        )

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context)
        assert result["sections"] == {}
        assert result["section_errors"]["posts"]["issue_template_path"] == (
            "/tmp/company-posts-issue.md"
        )

    async def test_get_company_posts_omits_orphaned_references(
        self, mock_context, serve_extractor
    ):
        mock_extractor = MagicMock()
        mock_extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(
                text="",
                references=[
                    {
                        "kind": "company",
                        "url": "/company/testcorp/",
                        "text": "TestCorp",
                    }
                ],
            )
        )

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_posts")
        result = await tool_fn("testcorp", mock_context)
        assert result["sections"] == {}
        assert "references" not in result


class TestJobTools:
    async def test_get_job_details(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/jobs/view/12345/",
            "sections": {"job_posting": "Software Engineer\nGreat opportunity"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_job_details")
        result = await tool_fn(
            "https://www.linkedin.com/jobs/view/12345/",
            mock_context,
        )
        assert "job_posting" in result["sections"]
        assert "pages_visited" not in result
        mock_extractor.read_job.assert_awaited_once_with("12345")

    async def test_get_job_apply_url(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/jobs/view/12345/",
            "apply": {"type": "external", "url": "https://jobs.example.com/12345"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_job_apply_url")
        result = await tool_fn("12345", mock_context)
        assert result == expected
        mock_extractor.get_job_apply_url.assert_awaited_once_with("12345")

    async def test_search_jobs(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/jobs/search/?keywords=python",
            "sections": {"search_results": "Job 1\nJob 2"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_jobs")
        result = await tool_fn("python", mock_context, location="Remote")
        assert "search_results" in result["sections"]
        assert "pages_visited" not in result

    async def test_search_jobs_is_bounded_by_the_registered_timeout(
        self, mock_context, serve_extractor
    ):
        """The loop stops itself by the same figure FastMCP cancels on.

        Registered with a non-default timeout, because dropping the argument
        leaves the extractor budgeting against its own 180s default while
        FastMCP still cancels at 60s. The search would then be killed mid-page
        and every page already gathered thrown away, which is the loss the
        bound exists to prevent.

        What arrives is what is left of it: FastMCP starts its clock before
        this tool runs, and acquiring the browser is spent from the same 60s.
        A cold start of three seconds passed on as a full sixty is the same
        defect one layer down.
        """
        mock_extractor = _make_mock_extractor(
            {
                "url": "https://www.linkedin.com/jobs/search/?keywords=python",
                "sections": {"search_results": "Job 1"},
            }
        )

        from linkedin_scraper_mcp.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp, tool_timeout=60.0)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_jobs")
        await tool_fn("python", mock_context)

        passed = mock_extractor.search_jobs.await_args.kwargs["tool_timeout"]
        assert passed <= 60.0
        assert passed == pytest.approx(60.0, abs=1.0)

    async def test_the_search_budget_pays_for_the_browser_it_waited_on(
        self, mock_context
    ):
        """FastMCP starts its clock before this tool runs.

        Acquiring the browser is spent from the same figure, so passing it on
        whole leaves the extractor planning against time it no longer has. A
        cold start is where this bites: warm, the wait is nothing and the
        budget is the full timeout either way, which is why asserting the
        figure alone cannot see the defect.
        """
        mock_extractor = _make_mock_extractor(
            {
                "url": "https://www.linkedin.com/jobs/search/?keywords=python",
                "sections": {"search_results": "Job 1"},
            }
        )

        async def slow_start(*args, **kwargs):
            await asyncio.sleep(0.3)
            return mock_extractor

        from linkedin_scraper_mcp.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp, tool_timeout=60.0)

        tool_fn = await get_tool_fn(mcp, "search_jobs")
        with patch(
            "linkedin_scraper_mcp.tools.job.get_ready_extractor",
            side_effect=slow_start,
        ):
            await tool_fn("python", mock_context)

        passed = mock_extractor.search_jobs.await_args.kwargs["tool_timeout"]
        assert passed < 59.9

    async def test_get_saved_jobs(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/jobs-tracker/?stage=saved",
            "sections": {"saved_jobs": "Saved Job 1\nSaved Job 2"},
            "job_ids": ["111", "222"],
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.job import register_job_tools

        mcp = FastMCP("test")
        register_job_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_saved_jobs")
        result = await tool_fn(mock_context, max_pages=2)
        assert "saved_jobs" in result["sections"]
        assert result["job_ids"] == ["111", "222"]
        mock_extractor.get_saved_jobs.assert_awaited_once_with(
            max_pages=2, stage="saved"
        )


class TestGetMyProfileTool:
    async def test_get_my_profile_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/johndoe/",
            "sections": {"main_profile": "John Doe\nSoftware Engineer"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        result = await tool_fn(mock_context)
        assert result["url"] == "https://www.linkedin.com/in/johndoe/"
        assert "main_profile" in result["sections"]
        mock_extractor.get_my_profile.assert_awaited_once()

    async def test_get_my_profile_with_sections(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/johndoe/",
            "sections": {"main_profile": "John Doe", "experience": "Work history"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        result = await tool_fn(mock_context, sections="experience")
        assert "main_profile" in result["sections"]
        assert "experience" in result["sections"]
        call_kwargs = mock_extractor.get_my_profile.call_args.kwargs
        assert "experience" in call_kwargs["sections"]

    async def test_get_my_profile_passes_callbacks(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/johndoe/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        await tool_fn(mock_context)

        call_kwargs = mock_extractor.get_my_profile.call_args.kwargs
        assert "callbacks" in call_kwargs
        assert isinstance(call_kwargs["callbacks"], MCPContextProgressCallback)

    async def test_get_my_profile_unknown_section(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/in/johndoe/",
            "sections": {"main_profile": "John Doe"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        result = await tool_fn(mock_context, sections="bogus_section")
        assert result["unknown_sections"] == ["bogus_section"]

    async def test_get_my_profile_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_scraper_mcp.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.get_my_profile = AsyncMock(side_effect=SessionExpiredError())

        from linkedin_scraper_mcp.tools.person import register_person_tools

        mcp = FastMCP("test")
        register_person_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_my_profile")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn(mock_context)


class TestSearchCompaniesTool:
    async def test_search_companies_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/search/results/companies/?keywords=fintech",
            "sections": {"search_results": "Stripe\nFintech company\nSan Francisco"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_companies")
        result = await tool_fn("fintech", mock_context)
        assert "search_results" in result["sections"]
        mock_extractor.search_companies.assert_awaited_once_with("fintech")

    async def test_search_companies_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_scraper_mcp.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.search_companies = AsyncMock(side_effect=SessionExpiredError())

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "search_companies")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn("fintech", mock_context)


class TestGetCompanyEmployeesTool:
    async def test_get_company_employees_success(self, mock_context, serve_extractor):
        expected = {
            "url": "https://www.linkedin.com/company/anthropic/people/",
            "sections": {"employees": "Jane Doe\nResearch Engineer\nSan Francisco"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_employees")
        result = await tool_fn(
            "https://www.linkedin.com/company/anthropic/",
            mock_context,
        )
        assert "employees" in result["sections"]
        mock_extractor.get_company_employees.assert_awaited_once_with(
            "https://www.linkedin.com/company/anthropic/", keywords=None
        )

    async def test_get_company_employees_with_keywords(
        self, mock_context, serve_extractor
    ):
        expected = {
            "url": "https://www.linkedin.com/company/anthropic/people/?keywords=engineer",
            "sections": {"employees": "Jane Doe\nResearch Engineer"},
        }
        mock_extractor = _make_mock_extractor(expected)

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_employees")
        result = await tool_fn("anthropic", mock_context, keywords="engineer")
        assert "employees" in result["sections"]
        mock_extractor.get_company_employees.assert_awaited_once_with(
            "anthropic", keywords="engineer"
        )

    async def test_get_company_employees_error(self, mock_context, serve_extractor):
        from fastmcp.exceptions import ToolError

        from linkedin_scraper_mcp.exceptions import SessionExpiredError

        mock_extractor = MagicMock()
        mock_extractor.get_company_employees = AsyncMock(
            side_effect=SessionExpiredError()
        )

        from linkedin_scraper_mcp.tools.company import register_company_tools

        mcp = FastMCP("test")
        register_company_tools(mcp)

        serve_extractor(mock_extractor)
        tool_fn = await get_tool_fn(mcp, "get_company_employees")
        with pytest.raises(ToolError, match="Session expired"):
            await tool_fn("anthropic", mock_context)


class TestToolTimeouts:
    async def test_all_tools_have_global_timeout(self):
        from linkedin_scraper_mcp.server import create_mcp_server

        custom_timeout = 7.5
        mcp = create_mcp_server(tool_timeout=custom_timeout)

        tool_names = (
            "get_person_profile",
            "search_people",
            "get_company_profile",
            "get_company_posts",
            "get_job_details",
            "get_job_apply_url",
            "search_jobs",
            "get_saved_jobs",
            "close_session",
        )

        for name in tool_names:
            tool = await mcp.get_tool(name)
            assert tool is not None
            assert tool.timeout == custom_timeout

    async def test_all_tools_have_default_timeout(self):
        from linkedin_scraper_mcp.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
        from linkedin_scraper_mcp.server import create_mcp_server

        mcp = create_mcp_server()

        tool_names = (
            "get_person_profile",
            "get_my_profile",
            "search_people",
            "get_company_profile",
            "get_company_posts",
            "search_companies",
            "get_company_employees",
            "get_job_details",
            "get_job_apply_url",
            "search_jobs",
            "get_saved_jobs",
            "close_session",
        )

        for name in tool_names:
            tool = await mcp.get_tool(name)
            assert tool is not None
            assert tool.timeout == DEFAULT_TOOL_TIMEOUT_SECONDS
