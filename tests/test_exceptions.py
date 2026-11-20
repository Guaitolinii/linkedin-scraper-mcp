from linkedin_scraper_mcp.exceptions import SessionExpiredError


def test_session_expired_default_message():
    err = SessionExpiredError()
    assert "--login" in str(err)


def test_session_expired_custom_message():
    err = SessionExpiredError("custom")
    assert str(err) == "custom"
