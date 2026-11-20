# AGENTS.md

Guidance for AI coding agents working in this repository. This project is a read-only fork of linkedin-mcp-server (Apache-2.0); see NOTICE.

**Hard rule: this server must stay read-only.** Do not add any tool that sends, posts, connects, archives, dismisses or otherwise changes state on LinkedIn, and do not add outbound network calls to anything but LinkedIn. `tests/test_read_only_surface.py` enforces the tool list.

## Development Commands

- Use `uv` for dependency management: `uv sync` (dev: `uv sync --group dev`)
- Lint: `uv run ruff check .` (auto-fix with `--fix`)
- Format: `uv run ruff format .`
- Type check: `uv run ty check` (using ty, not mypy)
- Tests: `uv run pytest` (with coverage: `uv run pytest --cov`)
- Pre-commit: `uv run pre-commit install` then `uv run pre-commit run --all-files`
- Run server locally: `uv run -m linkedin_scraper_mcp --no-headless`
- Install browser: `uv run patchright install chromium`

## LinkedIn Page Rules

- **Voyager / private API.** Out of scope. [Read the rendered page](docs/decisions/2026-09-16-rendered-page.md).
- **One section = one navigation.** Each entry in `PERSON_SECTIONS` / `COMPANY_SECTIONS` (`linkedin/fields.py`) maps to exactly one page navigation. Never combine multiple URLs behind a single section.
- **Minimize DOM dependence.** Prefer innerText and URL navigation over DOM selectors. When DOM access is unavoidable, use minimal generic selectors (`a[href*="/jobs/view/"]`) — never class names tied to LinkedIn's layout.
- **Throttling is a status, not a sentence.** LinkedIn's 429 (and its own 999) is read from the responses the page receives, never from words on the page, which are translated. `core/throttle.py` records them for the length of one call, and a call that fails says what LinkedIn refused, even when its error would otherwise be masked. [Read the decision](docs/decisions/2026-09-23-throttling-is-a-status.md).
- **Detection must be locale-independent.** Classification logic — connection state, action availability, button identity — must rely on URL patterns (`/preload/custom-invite/?vanityName=USER`, `/in/USER/edit/intro/`, `/messaging/compose/`), attribute *presence* (`aria-label` exists, `aria-expanded` exists, `aria-disabled` exists), or structural counts — never on text values like "Connect", "Follow", "Message", "1st", "Pending". The verb in an `aria-label` is locale-dependent; whether the attribute exists is not. Where text is genuinely the only signal, guard it behind an explicit per-locale table and document the limitation in code.

## Browser Identity Rules

- **The browser must not contradict itself.** Anything it says about itself has
  to survive being checked against another surface of the same browser: the
  user-agent against `sec-ch-ua`, the page against its workers and iframes, the
  reported screen against the window sitting on it. The goal is coherence, not
  invisibility — invisibility cannot be proven, while a contradiction is a fact
  and can be found by anyone who looks twice.
- **Never inject a fingerprint.** No `user_agent`, no custom headers, no
  spoofed client hints. Patchright's own guidance says the same, and every
  override measured here made things worse: a `user_agent=` argument changes
  the string but not the client hints, and never reaches service workers at
  all ([playwright#5237](https://github.com/microsoft/playwright/issues/5237),
  closed as an upstream Chromium bug). A browser telling the truth beats one
  caught lying.
- **A proxy must contain every egress path, not just HTTP.** WebRTC uses UDP
  and went around a configured proxy until the switches in
  `browser_launch.py`. DNS and QUIC belong to the same family; check them
  before assuming a new setting is contained.
- **Verify identity changes by measurement.** `docs/browser-fingerprint.md`
  lists the four detectors, what each one alone would miss, and the values
  measured so far. A launch-configuration change without a measurement against
  them is a guess.

## Profile Safety Rules

- **Nothing is moved or deleted under a root the server cannot prove it owns.**
  Every operation that calls `rmtree`, `shutil.move`, `unlink` or `rename` on a
  user-supplied path goes through `_owned()` in `session_state.py`, which
  answers from `profile_claim.require_profile_claim`. Adding a destructive
  operation without routing it through that is the one change this rule exists
  to catch: `USER_DATA_DIR` accepts any path, and a mistyped one costs a
  directory nobody meant to name.
- **Guard the configured source root, once, before any short-circuit.** Not the
  derived paths, which are computed from a root that already passed, and not
  after an exists-or-empty check, which a foreign directory reaches without ever
  being judged. A check on a derived runtime profile is worse than none: it
  asks about a nested auth root the server deletes on purpose, while reading as
  protection.
- **The auth root is the blast radius, not the profile.** `cookies.json`,
  `source-state.json`, `runtime-profiles/` and every `invalid-state-*` live one
  level *above* `USER_DATA_DIR`, so the emptiness of the profile says nothing
  about what a rotation takes with it.
- **Expand and resolve together, always.** Doing one without the other lets a
  symlink move the profile out of one directory while its sidecars come from
  another. Use `session_state.canonical()`.
- **A browser older than the profile is refused, and only that direction.**
  `browser_downgrade.refuse_a_downgrade()` runs in `BrowserManager.start()`
  before anything is created or opened. Chromium does not stop a downgrade on
  macOS or Linux: it opens the profile and lets each store decide for itself,
  and a store that answers `INIT_TOO_NEW` is dropped in silence. Losing the
  cookie store that way looks exactly like an expired session. Every unknown
  here fails open (an unreadable marker, a binary that will not name itself),
  because not knowing is not evidence. That trade is one-shot per profile: the
  older browser then rewrites `Last Version` down to its own number, so the
  evidence is gone for good, which is why those two branches warn.
- **Never ask a Windows browser for its version.** Chromium compiles
  `HandleVersionSwitches` only under `BUILDFLAG(IS_POSIX)`, so there `--version`
  is an unrecognised switch and the binary starts a browser on whatever profile
  it defaults to. The guard is off on Windows on purpose; a version could only
  come from the executable's file-version resource.
- **Two versions only compare inside one product.** `Last Version` records a
  number and no product, so a comparison across products compares two
  numbering schemes: Vivaldi is on 7.x and Edge's build number sits an order
  of magnitude below Chrome's under the same major. Only the Chrome-family
  names in `_COMPARABLE_PRODUCTS` are compared, matched **whole and never as a
  prefix** — a prefix scan accepted a launcher script announcing itself as
  `Chromium launcher 1.2.3` and refused the newer browser behind it. And only
  for the *running* binary, which is all `--version` can identify. A profile
  written by a fork is therefore still refused; that one is not repairable from
  `Last Version`, and the error says so by naming the number to go back to
  rather than a browser.
- **Never trim `_COMPARABLE_PRODUCTS` to one name.** `uvx` and `pip` resolve
  the declared patchright range fresh, and the two managed names split across
  it: at the declared floor every platform reports `Chromium`, revision 1200
  moved macOS and Linux x64 to `Google Chrome for Testing`, and Linux arm64
  followed only at patchright 1.63.0. So both names are in the field on any
  release, even when every published image reports the same one, and dropping
  either turns the guard off for a supported install. Which name a platform
  reports at a given revision is a measurement, never an inference from the
  version number. The third entry, `google chrome`, is not a managed
  browser at all but what an operator's own binary reports under `CHROME_PATH`,
  and it earns its place only when *that* Chrome is the older one: the guard
  reads the running binary, never the profile's writer. `browsers.json` is not
  evidence here: its `title` key dates from patchright 1.58.0 and omits the
  `Google` the binary prints. See `_COMPARABLE_PRODUCTS` for the measurements.

## Tool Return Format

All tools that read LinkedIn return: `{url, sections: {name: raw_text}}`.

Optional additional keys:

- `references: {section_name: [{kind, url, text?, context?, value?}]}` — LinkedIn URLs are relative paths; `value` carries non-URL identifiers (e.g. company URN id for `kind: "company_urn"`)
- `section_errors: {section_name: {error_type, error_message, issue_template_path, runtime, ...}}`
- `unknown_sections: [name, ...]`
- `job_ids: [id, ...]` (search_jobs and get_saved_jobs)
- `total: {count, exact}` (search_jobs only) — the result count LinkedIn advertises on the first page; `exact` is false for a lower bound such as "500+"
- `promoted_job_ids: [id, ...]` (search_jobs only) — the subset of `job_ids` shown as promoted; present only when every page could be read, so an empty list means none were
- `pagination: {pages_fetched, next_offset, reason}` (search_jobs only) — why paging stopped: `complete`, `max_pages`, `time_budget` or `no_new_ids`; `next_offset` is the `start` the next page would have used. Absent when a failure stopped the search, which `section_errors` then explains; a `filters_dropped` or `no_matching_jobs` entry is not a failure and keeps it
- `references["feed"]` (get_feed only) — every entry is `kind: "feed_post"`; non-post anchors (sidebar profiles, employer logos) are filtered. URLs may carry either `/feed/update/<urn>/` (DOM-anchor-derived) or `/posts/<slug>` (SDUI-derived) form; both are valid LinkedIn permalinks. An entry paired with a rendered post carries that post's URN as `value` and may carry `text` (an excerpt of the post); a post whose own markup does not name exactly one post URN is not paired, rather than given a guessed URL. Cap is 50 entries, matching `get_feed`'s `num_posts` ceiling.
- `references["search_results"]` (search_posts only) — DOM references first, then up to 50 `kind: "feed_post"` permalinks read from the page's JSON/document payload responses (`/feed/update/<urn>/` or `/posts/<slug>`, both valid). Captured permalinks are appended, not aligned to result order.
- `apply: {type, url?}` (get_job_apply_url, which returns no `sections`) — `type` is `easy_apply`, `external`, `applied`, `closed` or `unknown`; `url` is the employer's application link as LinkedIn gives it, never opened, present for external postings
- `apply: {type}` (get_job_details) — `type` is `applied` or `closed`, read from the posting's text above its description without clicking; absent for an open posting and for one whose description heading was not captured
- `reply_to_conversation` returns no `sections`: `{url, status, message, recipient_selected, sent, retry_safe, thread_id, participant}`, the `send_message` fields plus the thread and the name its title shows (null when unread)
- `archive_conversation` returns no `sections`: `{url, thread_id, participant, status}`, with `status` `archived` or `already_archived`; anything else raises
- `dismiss_job` returns no `sections`: `{url, job_id, keywords, dismissed: true, already_dismissed}`, where `url` is the worldwide search for `keywords`, the title and company its posting names
- `form: {steps: [{heading?, progress?, fields: [{label, type, required, options?, options_total?, selected?, value?, max_length?, error?}]}], complete}` (get_easy_apply_form, which returns `apply: {type}` beside it and no `sections`) — present for Easy Apply postings only; `complete` is false when steps after the last one were not read, and `error` marks the fields LinkedIn refused on that step

A failed call is an error result whose `_meta["linkedin.dev/error"]` is `{v, code, retry_after?}`. `ErrorCode` in `error_handler.py` is the closed set. Raise a failure through `raise_tool_error` or as `CodedToolError`, and build an error result the server returns instead of raising with `error_result`; a bare `ToolError` reads as `internal`.

## Tests

- Tests must write artifacts only to temporary directories, including in subprocesses.
- **Tautologies.** Assert an observable contract independent of the
  implementation. Before committing a test, mutate the covered behaviour
  to introduce a plausible regression and watch that test fail. Reject
  language restatements and redundant assertions. Pin an always-loaded
  instruction pointer. A sentence in a disclosed doc is not a test.
  For scroll-stop tests, let one batch land per round so a premature
  stop fails the test.
- **Browser-DOM tests belong where the unit suite mocks `page.evaluate`.**
  Extractor JS never executes under a mock, so a `browser_dom` test is its
  only coverage. Prefer a unit test elsewhere, and keep in mind that a
  fixture imitating LinkedIn's markup is a claim about LinkedIn, while one
  driving a synthetic container is a claim about the algorithm only.
- **Reproduce a timing flake at its seam.** Inject the stall where the code
  waits, with a monkeypatch or a `time.sleep` in a scratch copy, and run only
  the affected tests at `-n 4` at most. Agents share one machine; one session
  owns full-suite runs.

## Live Request Limits

Live checks share one LinkedIn account, so every session counts toward the
same limits. One tool call can cost several browser actions:
`get_person_profile` loads one page per section and a second one for a
section LinkedIn rate-limits, `send_message` takes three, and
`connect_with_person` up to seven.

- Per tool: at most 10 calls a minute and 100 a day.
- Profiles: at most one page load a second for `get_person_profile` and
  `get_company_profile`, counted per section.
- Invitations: at most 30 a day, 10 seconds apart. Count every outgoing
  invitation attempt, whatever it returns: `send_failed`, an error, or
  `outcome_unknown` may still have sent one.
- On a login challenge, a CAPTCHA, or a rate-limit page, stop all live checks
  for 24 hours.
