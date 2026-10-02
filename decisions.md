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

## 2026-10-02: Manager and subagent working mode

Claude plans, writes the brief, reviews the diff, re-runs the checks and keeps the
plan files current. A subagent writes the code. One plan step per round, then stop
and report. Changes stay uncommitted unless the user says otherwise.

## 2026-10-02: Recovery catches Playwright's error class, plus a message check

**Decision.** `_goto` catches `playwright.sync_api.Error` instead of `Exception`, and
still checks the message for "crash" or "closed" before relaunching.

**Why.** The project rules require specific exceptions. The message check has to stay
because Playwright's `TimeoutError` is a subclass of the same class and must not
trigger a relaunch. Re-verified live: killing the renderer and killing the browser
process both recover.

**Consequences.**

- `playwright` is now a direct dependency in `pyproject.toml` (it was only pulled in
  by cloakbrowser), kept at the locked 1.60.0.
- Recovery would not fire if cloakbrowser were switched to its `patchright` backend,
  whose errors are a different class. The project does not use that backend.

## 2026-10-02: Rules applied to changed code only

The retrofit covered the functions touched by the resilience work, not the whole
file. Older code keeps its ruff and mypy findings until a dedicated cleanup (plan
step 6), so diffs stay small and reviewable.

`[tool.mypy]` sets `explicit_package_bases = true` because `src/` has no
`__init__.py`; without it mypy refuses to run.

## 2026-10-02: A GitHub rate limit is an exception, not a `False`

**Context.** `check_and_download_release` returned `False` for both "up to date" and
"failed", so a rate limit showed up as "missing the patches JSON" in add-source and
not at all after "Sync Sources".

**Decision.** It raises `GitHubRateLimitError` (403 or 429 with `retry-after` or
`x-ratelimit-remaining: 0`). `update_tools` stops at the first one, saves what it
already synced and re-raises. `add_source` answers HTTP 429, the build prints the
message and carries on with the files on disk, and the background sync stores it in
`SYNC_ERROR` for `/api/ui/tools_status`.

**Why.** Only the rate limit needed telling apart, so the return value stays as it
was for every other case. Stopping at the first hit avoids spending more requests
that would fail the same way.

**Trade-offs.**

- A 403 or 429 without those headers is still reported as a generic failure.
- If a tool jar was downloaded just before the limit hit, that build does not treat
  the tools as updated, so apps already at the current version are not re-patched.
- The reset time is shown in UTC.

## 2026-10-02: One module owns `config.yaml`, with an in-process lock

**Context.** Request handlers, scheduler jobs and the pipeline each loaded the config,
changed it and wrote the whole thing back, unlocked and non-atomically. A build held
its snapshot for minutes and overwrote anything the user changed meanwhile.

**Decision.** `src/config_store.py` owns all config I/O. Changes go through
`update_config(mutate)`: load the current file, apply a small in-memory change, replace
the file atomically, all under one `threading.RLock`. The pipeline writes only the
fields it owns (per-app `status`, `error`, `last_successful_build`; source and tool
versions) and skips an app that no longer exists. Slow work (network, scraper, `java`)
always happens outside the lock.

**Why.** The app is one process, so a thread lock is enough and needs no dependency.
Applying small changes to the current file removes the lost updates at the root,
where merging snapshots would only paper over them. It also ended the duplicate
`load_config` and the `patcher -> main` back-import.

**Trade-offs.**

- Safe only with a single uvicorn worker. More workers would need a file lock.
- The atomic replace needs `/data` mounted as a directory. Mounting `config.yaml` as a
  single file would make every save fail.
- A build still works from the snapshot it loaded at the start: an app edited mid-build
  is built with its old patch selection, and the result is recorded on the edited
  entry.
- An update that changes nothing no longer rewrites the file.

## 2026-10-02: A build records the patch selection it used

**Context.** The pipeline skipped an app whenever the app version and patch tag were
unchanged and the APK existed, so editing an app's patches never produced a new APK.

**Decision.** `last_successful_build.patches` stores the sorted include and exclude
lists the build used. The pipeline skips only when they equal the current selection,
and `/api/ui/updates` reports an update when they differ.

**Why.** Comparing recorded state with wanted state also covers hand edits of
`config.yaml` and edits made while a build is running, which a "dirty" flag set by
the edit endpoint would miss.

**Consequences.**

- Builds made before this change have no recorded selection, so every app is rebuilt
  once on the next build.
- The version string served to Obtainium does not change when only the selection
  changes, so Obtainium does not offer the rebuilt APK as an update. That belongs to
  the open "Obtainium misses patch updates" item in plan step 4.

## 2026-10-02: Version string carries a hash of the build inputs

**Context.** Obtainium notices an update only when the version string or filename
changes. The string was `<app version>-<Morphe tag>`, so a new release of a
third-party source or a changed patch selection produced a new APK that Obtainium
never offered.

**Decision.** The string is `<app version>-<primary tag>-<8 hex>`. The primary tag is
the Morphe source's version when it was used, else the first used source's. The hash
is the first 8 hex digits of a SHA-256 over the versions of the sources actually used
and the patch selection. Whether to skip a build is decided from the recorded inputs
(`is_build_current`), not from the name, because the sources used are only known
after patching.

**Why.** The string changes exactly when the APK's inputs change, and stays put when
an unrelated source or a tool jar is updated. The prefix keeps it readable.

**Assumption, not verified.** Obtainium treats any change of the string or filename
as an update and does not need the strings to sort.

**Consequences.** On the first build after upgrading every app gets a new string, so
Obtainium offers one update per app.

## 2026-10-02: Patch into a scratch directory, move in on success

**Decision.** `patch_app` writes to `apks/.building/<tmp>/`. A successful result is
moved into `apks/patched/` with `os.replace`, recorded, and only then are the app's
older APKs removed. `.building` is wiped at the start of every build.

**Why.** The old code deleted the served APK before patching, so a failed build left
nothing to download. `.building` sits next to `patched` so the move is a rename on
the same filesystem, and no `<pkg>-*.apk` glob over `patched` can see a partial file.

**Trade-off.** `apks/patched` must not be a separate mount from `apks`, or the move
fails and the build is recorded as an error.
