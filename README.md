# linkedin-scraper-mcp

A **read-only** MCP server that lets an AI assistant research LinkedIn profiles, companies and jobs through **your own browser session**. It can look things up on its own. It cannot send messages, send connection requests, or change anything on your account.

> Independent open-source project. Not affiliated with, endorsed by or sponsored by LinkedIn or Microsoft. LinkedIn is a trademark of LinkedIn Corporation.

This is a modified version of [linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server) by Daniel Sticker, used under the Apache License 2.0. See [What changed](#what-changed-from-the-original) and [NOTICE](NOTICE).

## Tools

All 13 tools only read.

| Tool | Description |
|------|-------------|
| `get_person_profile` | Read profile sections: experience, education, skills, projects, volunteering, posts. |
| `get_my_profile` | Read your own profile, with the same section selection. |
| `search_people` | Find people by keyword, location, degree, company and mutual connections. |
| `get_company_profile` | Read company information, with optional sections for posts and open jobs. |
| `get_company_posts` | Read recent posts from a company's feed, with links to the originals. |
| `get_company_employees` | Find company employees, optionally filtered by name, title or skill. |
| `search_companies` | Find companies by keyword and return links to their profiles. |
| `search_jobs` | Find job postings by keyword and location, with job IDs for details. |
| `get_job_details` | Read a posting's description, requirements and company details. |
| `get_job_apply_url` | Check whether a job uses Easy Apply or links to the employer's application. |
| `get_saved_jobs` | List your saved job postings. |
| `get_session_status` | Check your saved login and browser state without opening a browser. |
| `close_session` | Close the shared browser without deleting your saved login. |

## Install

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12 or newer.

```bash
uv tool install git+https://github.com/Guaitolinii/linkedin-scraper-mcp
```

or, from a local checkout:

```bash
uv tool install .
```

> **Do not run `uvx linkedin-scraper-mcp`.** That name on PyPI belongs to a different, unrelated package, and `uvx` would install and run its code. Always install from this repository, as above.

Pin to a tag or commit when you want updates to be a decision rather than a surprise:

```bash
uv tool install git+https://github.com/Guaitolinii/linkedin-scraper-mcp@<tag>
```

## Log in

```bash
linkedin-scraper-mcp --login
```

A browser window opens. Sign in to LinkedIn yourself (2FA and captchas included). The session is saved under `~/.linkedin-scraper-mcp/profile` and reused until it expires. On Windows this is the reliable path.

Optional shortcut: if you are already signed in to LinkedIn in Chrome, Edge, Brave and similar browsers, `linkedin-scraper-mcp --import-from-browser` copies that session instead. Recent Chrome and Edge versions encrypt cookies in a way that cannot be read without elevation (app-bound "v20" encryption), so on Windows this often fails and `--login` is what works.

To forget the session: `linkedin-scraper-mcp --logout`.

## Use it from an MCP client

Claude Code:

```bash
claude mcp add linkedin -- linkedin-scraper-mcp
```

Any client that takes a JSON config:

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "linkedin-scraper-mcp"
    }
  }
}
```

The first tool call after login starts a Chromium browser; the first run may download it.

## Pacing

An assistant that researches on its own can issue many calls in a row. Each one loads real LinkedIn pages, and bursts are what get accounts throttled. This server spaces calls out. It only ever delays a call, never refuses or retries one.

| Variable | Default | Meaning |
|----------|---------|---------|
| `LINKEDIN_MIN_CALL_GAP_SECONDS` | `3` | Minimum quiet gap between the end of one call and the start of the next. |
| `LINKEDIN_CALL_GAP_JITTER_SECONDS` | `1.5` | Random extra added to that gap, so the rhythm is not machine-regular. |
| `LINKEDIN_MAX_CALLS_PER_MINUTE` | `12` | Ceiling on calls per rolling minute. `0` turns the ceiling off. |

Set the first two to `0` and the third to `0` to switch pacing off entirely. Other settings (headless mode, timeouts, log level, profile directory) are listed in [`.env.example`](.env.example) and in `linkedin-scraper-mcp --help`.

## What changed from the original

- **Read-only.** The code for these tools was deleted, not disabled: sending and replying to messages, connection requests, archiving conversations, dismissing jobs, opening Easy Apply forms, and reading your inbox, conversations, feed, post search and sidebar suggestions.
- **No outbound calls except to LinkedIn.** The original contacted `mcp.daniel.sticker.name` at every start (sending version, OS, architecture, language and the name of your MCP client) and accepted remotely served tools whose calls were posted back to that server. It also checked PyPI for updates and searched GitHub issues with your error text. All of that is removed. Browser installation still downloads Chromium from its official source.
- **Pacing** as described above.
- **Renamed**: package `linkedin_scraper_mcp`, command `linkedin-scraper-mcp`, state in `~/.linkedin-scraper-mcp`. It does not share a login or state with the original, and the two can be installed side by side.
- **Trimmed** the original's release, CI, Docker and marketplace-plugin machinery, which does not apply here.

## Responsible use

Automating LinkedIn through a browser is against LinkedIn's terms of use, and it can get your account limited or banned. You use this software at your own risk. Keep volumes low, keep pacing on, use it on your own account for your own research, and respect other people's privacy and applicable law (for example GDPR and LGPD when you handle personal data).

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
uv run linkedin-scraper-mcp --no-headless   # run locally with a visible browser
```

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
Original work Copyright 2025-2026 Daniel Sticker. Modifications Copyright 2026 Gustavo Guaitolini.
