# Decisions

Record of important choices and why they were made.

## 2026-10-01: Relaunch the whole browser on any crash, not just the page

**Context.** Builds failed with `Page.goto: Page crashed`. The scraper keeps one page
for the life of the process, so one crash broke every later lookup until restart.
An earlier hot-patch recreated only the page, which did not survive a dead browser.

**Decision.** On a navigation error that mentions a crash or a closed target, close
what is left, launch a fresh browser and retry once.

**Why.** One recovery path covers both a dead renderer and a dead browser, and a new
browser also resets any memory built up over days of scraping. Relaunch costs about
a second.

**Trade-off.** Recovery happens only at navigation. A crash in the middle of a search
fails that one search; the next one recovers.

## 2026-10-01: Keep the original APKMirror search URL

**Context.** A hot-patch removed the arch/dpi/bundle filters and quoted the query, on
the theory that the filtered page was heavy enough to crash the renderer.

**Decision.** Reverted to the committed URL.

**Why.** Measured: the page is 373 KB with the filters and 365 KB without. The URL
from the failing log loaded locally in under three seconds. The theory was wrong, and
the committed URL is the known-good behaviour.

## 2026-10-01: Read the newest version from the search results

**Context.** Finding the newest version walked search -> release -> variant page,
hourly, for every unpinned app.

**Decision.** `latest_version` reads the version from the first matching search row.

**Why.** One page load instead of three, so less exposure to crashes and blocking.
Checked that the value matches the download page's version string.

## 2026-10-01: Scraper logs go to the current stdout

**Context.** The build console showed only "Scraper failed" because scraper logs went
to stderr while the console captures stdout.

**Decision.** The scraper logger uses a handler that prints to whatever `sys.stdout`
is at the time, and does not propagate to the root logger.

**Why.** The build swaps `sys.stdout` for a tee; a handler bound at import time would
keep writing to the old stream. Limiting it to the scraper logger keeps other
libraries' logs out of the build console.

## 2026-10-01: Root cause of the container crash is still open

`/dev/shm` is ruled out: the browser is already launched with
`--disable-dev-shm-usage`. The crash did not reproduce outside Docker. Candidates are
a container memory limit, memory growth in the long-lived page, or the old Chromium
build in cloakbrowser 0.3.31. Not bumping cloakbrowser until this is clearer, because
it is a guess and a larger change.

## 2026-10-01: No TypeScript in this project

The global "TypeScript everywhere" rule does not apply here. The backend stays Python
and the frontend stays inline JavaScript in the Jinja template. The Python rules in
the project `CLAUDE.md` are the ones that apply.

## 2026-10-01: Retrofit the scraper changes to the Python rules, then continue

**Context.** The project `CLAUDE.md` gained strict Python rules (pytest tests, type
hints, specific exceptions, 88-character lines) after the scraper changes were
written in the style of the surrounding code.

**Decision.** Bring the scraper changes up to the rules and add pytest tests first
(plan step 1a), then do the add-source flow and everything after it under the same
rules.

**Why.** Otherwise the newest code in the repo would already break the rules, and
the recovery logic has no repeatable test; it was only verified with throwaway
scripts against live APKMirror.

## 2026-10-01: Work order

Scraper resilience first, then the add-source flow, then config safety, then the
remaining bugs. The first two are the reported problems; config safety comes before
the rest because builds overwriting UI changes can undo any other fix.

## 2026-10-02: add_source syncs before saving

A source is only written to `config.yaml` after its first sync succeeds. A bad repo returns
HTTP 400/502 and leaves no half-added entry. Repo input is reduced to `owner/repo` by
`normalize_repo`, and duplicates compare case-insensitively.
