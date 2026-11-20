#!/usr/bin/env python3
"""Entry point for the ``linkedin-scraper-mcp`` command and ``python -m``."""


def main() -> None:
    """Import and run the server."""
    from linkedin_scraper_mcp.cli_main import main as run_server

    run_server()


if __name__ == "__main__":
    main()
