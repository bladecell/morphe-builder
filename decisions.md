# Decisions

## 2026-10-14: Neo-brutalism admin UI, light and dark

- Restyled `index.html` to the owner's Neo-brutalism spec (DESIGN.md). Components are
  `nb-*` classes in `tailwind/input.css` driven by CSS variables; the markup carries
  no `dark:` variants. Light and dark follow the system setting (`darkMode: "media"`,
  no toggle, no `class="dark"`).
- Space Grotesk is vendored in `src/static/fonts/` (SIL OFL 1.1, fontsource 5.3.0
  files, latin subset, 500 and 700). The family has no 900 weight, so 900 requests
  render the 700 face. No CDN. sha256: 500 `1b1a8131d9edf975d9decee81e2f2bf504812f7a4f498e5500f28a613e22e64c`,
  700 `35f8aec56cfd5cbfdb03cc68733a54a0b05bb3617ffcd5fd332badc0b045ca55`,
  OFL.txt `18a4de52385f6b988782639d5d0cc1326e5a8c2de9a7f01d7b20d9aedcc60943`.
- Rotation only on the logo, two headings and badges; marquees, floaters and extra
  icons rejected. Tables, forms, patch lists and dialogs stay straight.
- Deviations from the spec: no grays, so disabled controls use opacity 0.55; muted
  text is weight 500 instead of a gray; placeholder is ink at 65% (the spec's 40% is
  below AA); success/info/status badges reuse violet because the palette has no green.

## 2026-10-14: Replace cloakbrowser with ShardX

- `src/apkmirror_downloader.py` now drives ShardX (`shardx==2.0.3`, MIT, github.com/ProxyShard/ShardBrowser):
  engine launched headless through its SDK with `cdp=True`, attached by sync
  Playwright `connect_over_cdp`, throwaway profile per launch, `--no-sandbox` as
  root, engine fetched at Docker build time only.
- Reason: in Docker, cloakbrowser could not pass APKMirror's Cloudflare managed
  challenge (403 forever). ShardX headless passes it; headed under Xvfb did not.
- No licence key is needed. The `CLOAKBROWSER_LICENSE_KEY` secret, Settings field,
  rejection status and keyless fallback were removed. Startup strips a stale key
  from `.env`/environment and deletes `DATA_DIR/bin/cloakbrowser`.
- Also in tree: Morphe API variant resolver and an `apkeep` APKPure fallback
  (hash-pinned in the Dockerfile). Uptodown was added later; see 2026-10-14.
- Risk: the ~170 MB Chromium engine comes from a ProxyShard CDN and is unsigned.
  It is fetched at build time only; see the Dockerfile for the verification done.
- Earlier cloakbrowser entries below are kept as history and marked superseded.

## 2026-10-11: HushFacebook requires Android API 30 artifacts

Approved policy: constrain `com.facebook.katana` builds only when resolved,
selected providers include `SysAdminDoc/HushFacebook`, matching repository
identity case-insensitively. Filter APKMirror variants by the row's Android
minimum-version label before existing architecture/DPI/bundle scoring, then
verify the downloaded or cached APK's manifest `minSdkVersion` with APKEditor.
Uploads use staged validation and atomic replacement, with the current source
policy rechecked under the config write lock. Builds patch a private copy of the
exact APK they inspected, rather than rereading the mutable raw-cache path.
Missing or ambiguous variant metadata and uninspectable artifacts fail closed;
cached inspection failures do not trigger silent redownload. Other apps retain
legacy behavior. This validates the minimum SDK floor, not DEX-variant identity
or complete patch compatibility.

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

## 2026-10-02: The base source is `MorpheApp/morphe-patches`, matched exactly

Version selection, the primary tag of the version string and the fallback bundle in
`patch_app` all recognise the base source through `is_base_repo`: a case-insensitive
exact match on the repo. Before, version selection used the display name
"Morphe Patches" and the version string used a substring match, which also treated
forks named `*/morphe-patches` as the base. The frontend still uses the display name
(open item in plan step 4).

## 2026-10-05: An unreadable patch list is an error, not a skipped source

**Context.** A truncated or malformed per-source patch JSON was swallowed by a bare
`except`, so the source vanished and the build picked the newest APKMirror version,
which may not match the patches. Truncated files came from downloads written
straight to their final path.

**Decision.** `load_patch_list` raises `PatchListError` naming the repo. A build of
the app stops with that error on its card and the previous APK stays served; the
updates route reports it per app; the patch dialog answers 502 with the message.
Downloads go to a `.part` file and are moved into place when complete.

**Known limits.**

- Any unreadable active list stops every unpinned app and fails the patch dialog for
  every app, because a truncated file cannot say which apps it covers and the saved
  selection does not yet name its source. To be narrowed with the per-source
  selection.
- A sync does not repair it: at an unchanged release tag with the file present
  nothing is downloaded. The file has to be deleted from `bin/`, or the source has to
  publish a new release.

## 2026-10-05: `add_app` validates the package id like the upload route

An id that does not match `PACKAGE_ID_PATTERN` gets HTTP 400 and nothing is written,
which keeps quotes, path separators and glob characters out of the template and the
file paths. Invalid ids already in a hand-edited `config.yaml` are not handled;
`delete_app` stays unvalidated so they can be removed.

## 2026-10-05: The frontend gets the base source from the backend

`GET /api/ui/patches/{id}` carries `is_base` per patch, decided by repo. The dialog no
longer compares the display name.

## 2026-10-05: No authentication; the whole app stays open

Decided by the user. The admin UI and `/api/ui/*` stay unauthenticated, like the
Obtainium endpoints. The app is expected to run on a trusted network; anyone who can
reach it can change sources, apps and settings and trigger builds.

## 2026-10-05: The patch selection will carry the source of each patch

Decided by the user, for the "patches hidden across sources" item in plan step 4.
Two sources can ship a patch with the same name for one app; both must be selectable.
The stored format of the selection in `config.yaml` changes.

**Design, approved 2026-10-05.**

- `include_patches` becomes a mapping from repo to patch names. The repo is the
  identity of a source: it is unique in the config, and display names are neither
  unique nor stable.
- The old flat list stays valid indefinitely and is never converted. A bare name is
  resolved at build time to the first active source in config order that provides
  it, which is the behaviour before this change. An app gets the new shape only when
  it is saved from the UI.
- Both copies of a same-named patch may be selected at once.
- Selections for a deleted or inactive source are kept and skipped at build with a
  warning, so re-adding the source restores them.
- The `morphe-cli` command is built with each bundle followed by its own selectors.
  This is done first, as its own step, after checking the behaviour against the
  released jar: the CLI's documentation and source say `-e` and `-d` apply to the
  bundle they follow, which would mean the current command (all bundles, then all
  selectors) applies the selection to the last bundle only.

**Trade-offs.**

- Rollback is silent: older code would read the repo strings as patch names and
  build a nearly unpatched APK. Copy `config.yaml` before upgrading.
- An app saved in the new shape rebuilds once and gets a new version string.
- The grouping fix can change what existing multi-source builds produce.

## 2026-10-10: Backend implementation of per-source selection (step 2)

The approved shape and compatibility rules above are implemented in the backend;
step 3's public API and dialog contract remain unchanged. Mapped selections for
deleted or inactive sources remain stored and are warned/skipped, but a mapping
with no applicable source fails before version lookup or APK download. Active
providers whose repo names alias the same local source files are rejected when
relevant to the selection; unrelated aliases do not block other mapped apps.
Legacy lists are not rewritten. Version recommendations use selected source
providers, aggregating the existing full compatible-version set per provider.

Local pytest and static checks are recorded in `todo.md`. No real morphe-cli,
live service, or browser validation was performed; independent review is pending.

## 2026-10-05: GitHub token is set through a Settings field that writes to `.env`

Decided by the user, for plan step 5. The token is never stored in `config.yaml`,
never logged and never returned; the UI shows only whether one is set.

## 2026-10-05: Bump cloakbrowser to 0.5.x, keyless unless a key is set

> Superseded 2026-10-14: cloakbrowser was replaced by ShardX; see the entry at the top.


Decided by the user. This reverses the 2026-10-01 note about not bumping. It is not
known to fix the container page crash; that cause is still open.

Without a licence key 0.5.x downloads the same Chromium 146 build as 0.3.31; the
newer Chromium needs a key (free with a GitHub sign-in, one browser session at a
time). The app runs keyless by default and uses a free key when the user enters one
in Settings. The key is a secret and is handled like the GitHub token: written to
`.env`, never to `config.yaml`, never logged or returned.

## 2026-10-05: The pipeline card shows every app error

Decided by the user. The card shows the stored error text of any app in "Error"
status, not only an unreadable patch list. The text is set as plain text. It can
contain raw exception text, including file paths.

## 2026-10-05: A sync repairs an unreadable patch list

Decided by the user. A sync downloads a source's patch list again when the copy on
disk fails to load. Accepted cost: while a list stays unreadable, each sync contacts
GitHub for that source despite the 6-hour throttle.

## 2026-10-05: Secrets live in `DATA_DIR/.env`, set through their own routes

> Cloakbrowser and licence-key parts superseded 2026-10-14 (ShardX switch, top entry).


Decided by the user, for the GitHub token and the cloakbrowser licence key.

- A new module `src/secrets_store.py` reads and writes `DATA_DIR/.env` (mode 0600,
  atomic). GitHub and cloakbrowser values use the existing validated secret
  reader; `APPRISE_URLS` uses its bounded JSON-array reader.
  Values are never copied into the process environment.
- Routes separate from "Save Settings": `GET /api/ui/secrets` returns only set /
  not set and the source; `PUT` and `DELETE /api/ui/secrets/{name}` set and clear.
- A real environment variable wins over `.env` and locks the Settings field
  (HTTP 409 on a change), so a network user cannot override the operator.
- A rejected licence key falls back to keyless and the status is shown; otherwise
  anyone could stop the scraper by saving a junk key.
- A rejected GitHub token (401) is reported and stops the sync. No retry without
  the token, since that hides a bad token.
- The token is sent to `api.github.com` only.
- `CLOAKBROWSER_AUTO_UPDATE=false` in the Dockerfile: no background downloads and
  no extra unauthenticated GitHub calls.
- CORS is tightened from `*` to same-origin. With `*`, any web page the user
  visits could overwrite or clear the secrets. This changes the 2026-10-05 "CORS
  left as is" note; the app still has no authentication.
- The upload route's lost-metadata bug (missing icon) was identified for follow-up;
  fixed 2026-10-10 as described below.

Known limits: the secret crosses the network in cleartext unless the app is
behind TLS; anyone on the network can overwrite or clear a secret that is not set
in the environment, but cannot read one back. Nothing with a real licence key
has been verified.

## 2026-10-10: Optional icon retrieval does not gate APK metadata

`extract_app_metadata` persists APKEditor name/version metadata independently of
optional APKMirror icon lookup/download. An icon miss or retrieval failure is
non-fatal. Icon downloads use a unique temporary path and are promoted with
`os.replace` only after success, so a partial file cannot block a retry; an
existing icon is left untouched. Cloakbrowser code-77 license denials still
propagate so the worker can reject the current task and recover.
The upload filename continues to use the submitted version; metadata continues
to use the APK's extracted version. Reason: failure to retrieve a display asset
must not discard valid metadata from a successfully inspected APK.

## 2026-10-05: Selectors follow their bundle; one forced rebuild; empty builds fail

`morphe-cli` v1.18.1 (checked by real runs) applies `-e` and `-d` only to the
`--patches` bundle they follow. The app used to list all bundles first, so builds
from several sources applied the selection to the last bundle only and still
reported success. `patch_app` now emits each bundle followed by its own selectors.

Decided by the user:

- The version hash gets a build-layout marker, so every app is rebuilt once and
  Obtainium offers one update per app. Without it the incomplete APKs would stay.
- A build fails when patches were selected but the CLI applied none, or when a
  selected name is unknown to every bundle. The CLI exits 0 in both cases. The
  previous APK is kept, as for any failed patch.
- Patch names are matched to sources ignoring case, as the CLI does. The command
  carries the spelling from the saved selection.

## 2026-10-05: Refuse cross-site and rebinding requests

Decided by the user after the security review of the secret routes. The app still
has no authentication; these two checks stop a web page the user visits from
acting on the app through the user's browser.

- State-changing requests whose `Origin` differs from the app's own address are
  refused. The discovery sync becomes a POST. Clients that send no `Origin`
  (Obtainium, curl) are unaffected. Behind a reverse proxy that rewrites `Host`,
  the server URL in Settings must be set.
- Requests are accepted only when `Host` is an IP address, `localhost`, the host
  of the server URL in Settings, or a name listed in the `ALLOWED_HOSTS`
  environment variable. Reaching the app by another hostname needs that name
  added.

Secret values are limited to letters, digits and `._+/=:@-`, so the `.env` file
never holds shell syntax.

## 2026-10-05: Guard fixes after the second security review

Decided by the user. The review found the guards fit after three fixes.

- Names in `ALLOWED_HOSTS` are also accepted as `Origin` hosts. Reason: behind a
  proxy that rewrites `Host`, every write was refused, including the one that
  sets the server URL. No separate `ALLOWED_ORIGINS` variable. The proxy should
  pass `Host` through; otherwise rebinding protection is the proxy's job.
- Every response forbids framing (`frame-ancestors 'none'`, `X-Frame-Options:
  DENY`). The app cannot be embedded in a dashboard frame.
- A loopback server URL is trusted as an `Origin` only when `Host` is loopback
  too. The default server URL stays `http://localhost:8000`.
- `/api/ui/updates` and `/api/ui/source_updates` stay GET; only one refresh runs
  at a time, and `source_updates` no longer writes `config.yaml`.
- Patch bundles run as Java code under the app's user and could read `.env`.
  Accepted with two measures: the GitHub token is documented as a fine-grained
  token with no permissions, and the Java process gets an environment without
  the secrets. The patcher does not get a separate user.

## 2026-10-05: Guard rules after the third security review

Decided by the user. These replace two points of the section above.

- A bare name in `ALLOWED_HOSTS` allows that `Host` only. An `Origin` is trusted
  through the variable only when the entry is a full origin, for example
  `https://morphe.example.com`. Reason: a bare name also trusted whatever else
  runs on ports 80 and 443 of that host.
- A proxy on the same machine that rewrites `Host` to loopback defeats the
  loopback and rebinding rules. No switch is added; the proxy must pass `Host`
  through, and the docs will say so.
- `GET /api/ui/source_updates` is deleted. The page never called it, and it made
  GitHub calls any site could trigger.

## 2026-10-05: Before the GitHub token is sent

> Cloakbrowser and licence-key parts superseded 2026-10-14 (ShardX switch, top entry).


Decided by the user after the security review of the hardening step.

- Both secret variables are read into memory at startup and removed from the
  process environment. Reason: cloakbrowser reads the licence key from the
  environment by itself, without validation, and the browser and its child
  processes inherited both secrets. The key is passed to the browser explicitly
  in the licence key step; until then a key from the environment does not
  reach the browser.
- An environment value that is invalid is never used and does not fall back to
  the value saved in Settings. The status says so by name.
- A token that has permissions is used, with a warning in the status. Reason: a
  patch bundle can read the token from disk, so its scope is the only control.
- An invalid source already in `config.yaml` is shown as a sync error, not only
  logged.
- A source whose local files would collide with another source is refused.
  Files are not renamed.
- Redaction is global: one hook on all log records and one `print` replacement
  in `patcher.py`. It matches exact values only.

## 2026-10-06: Release sync after the independent review

Decided by the user after three independent reviews of the branch.

- The bundle and the patch list of a release are staged and moved into place
  only when both arrived. A new release without a usable list leaves the old
  pair and the old recorded tag, and is retried. Reason: the token step had made
  that case record the new tag next to the old list.
- The list is fetched before the bundle. A `<repo>#incomplete` marker in
  `versions.json` covers the two moves; while it exists the next sync ignores the
  throttle and fetches again. The recorded tag is not touched, so version
  strings do not change.
- A patch list needs at least one entry with a text name. A stored list without
  one counts as unreadable and is repaired on the next sync.
- Candidate order: named assets, the repo list at the release tag, unnamed
  `.json` assets up to 256 KiB, then unnamed assets up to 2 MiB smallest first.
  Every patch-list download is limited on bytes received (8 MiB for named assets
  and repo lists).
- Release tags may contain `/`. Each segment is validated on its own, and an
  asset URL must equal the one built from the release's tag and the asset name.
  The tag is sanitised before it enters the APK file name; the hash still uses
  the raw tag, so names of existing builds are unchanged.
- No GitHub request uses `~/.netrc`, on any redirect hop. Redirects to plain
  http are refused.

## 2026-10-06: One build and one sync at a time, claimed at request time

- A build is claimed when it is requested and released only by its owner. A
  second request answers "Build already in progress". The manual job never
  counts as missed, and the cron setup removes only its own two jobs. Reason:
  the flag used to be raised when the worker started, so a queued build could be
  queued twice and one build could clear the flag of another.
- One tool and source sync at a time (`patcher.SYNC_LOCK`). A second Sync
  Sources request starts nothing. Adding a source during a sync is refused with
  409, because a request should not wait behind a sync of several minutes. A
  build waits for the running sync, so it never builds from half-written files.
- A refresh of the updates view that started before a build finished is not
  stamped as fresh (generation counter).
- The worker status carries `busy_since` and a "starting" state. The watchdog
  itself stays open.
- Shutdown is unchanged: the process ends when the running build ends. Making
  blocked callers return early would abandon a build half way.
- A revoked or expired token still stops every sync; it is shown as a lasting
  `github_token_rejected` field in the tools status. No fallback to requests
  without the token.
- Not now: builds of pinned versions and local APKs without the browser.

## 2026-10-06: Guard and secret rules after the fourth review

- `Sec-Fetch-Site: cross-site` is refused on every state-changing request, and on
  GET only under `/api/ui/`. Reason: `/view`, `/download` and `/api/apps` are
  opened by links from other sites and apps. Limit: browsers send the header
  only over https and on localhost, so plain `http://<LAN IP>` gets no benefit.
- A loopback server URL counts as an Origin only when its port equals the port
  of the `Host` header.
- The server URL is validated when it changes (`http(s)://host[:port]` plus an
  optional path prefix). An unchanged stored value never blocks a save.
- When `ALLOWED_HOSTS` lists at least one full origin, the server URL is ignored
  by the Host and Origin rules. Reason: any client on the network can change the
  server URL. Bare names do not switch it off: a bare name allows a `Host` only,
  so an install that names its proxy's `Host` there still needs the server URL
  for the `Origin` of its own page, and would lose every write otherwise.
- An empty server URL can be saved and means unset.
- A secret value is masked only once it is in effect (file, environment, or a
  successful write). Values in effect are never dropped; replaced values are
  kept up to 32. A refused or failed write adds nothing.
- Error texts and the build log no longer contain request input. Reason: masked
  output showed whether the input was a known secret.
- Apprise URL handling is implemented separately as a write-only secret list in
  `APPRISE_URLS`; legacy `config.yaml` values remain runtime fallback until
  explicit migration or a secret-list mutation persists and removes them.

## 2026-10-10: Apprise destinations are write-only secrets

- Store a bounded JSON array as `APPRISE_URLS` in environment or `.env`, with
  environment ownership taking precedence even when invalid.
- Import legacy config only on explicit migration. Never overwrite a different
  file value; after a successful secret write, cleanup failures leave that
  secret authoritative for retry.
- URL mutation and migration share a coordinator lock. The UI page omits legacy
  URL values; test notifications use the effective stored list and never accept
  destinations from the request body.
- Validate supported URLs locally with Apprise and temporarily mask URL and
  credential components around parser/delivery logging; do not register refused
  request values as process-lifetime secrets.
- The existing Settings Secrets UI does not yet edit notification URLs.

## 2026-10-06: The page loads nothing from a third party

Decided by the user: build the stylesheet ahead of time instead of pinning the
CDNs or vendoring the in-browser Tailwind script.

- Tailwind 3.4.17, the version the CDN served. Reason: Tailwind 4 changes the
  look of this page (button cursor, blur, shadows, corners, placeholder colour).
  A move to 4 is its own change with a visual check.
- The standalone Tailwind binary runs the build, downloaded by
  `scripts/build_css.py` into a gitignored cache and run only when its SHA-256
  matches the pinned value. Reason: no Node toolchain in the project; the
  `pytailwindcss` wrapper neither pins nor verifies the binary.
- The generated `src/static/app.css` is committed. Reason: running from source
  and the tests need no extra tooling, and the Docker image needs no build
  stage. A recorded digest of the build inputs (templates, config, input CSS,
  Tailwind version, CLI arguments) makes a test fail when the stylesheet is out
  of date; a second test rebuilds and compares when the binary is cached.
- Alpine 3.17.4 is vendored with the version in the file name, checked against
  the npm registry's integrity value and jsDelivr's hash.
- The stylesheet URL carries a hash of the file's content, because the app
  sends no cache header for static files.
- A missing stylesheet is logged and the page still renders, unstyled.
- Licences are in `THIRD_PARTY_NOTICES`.

## 2026-10-07: Settings UI for the GitHub token

> Cloakbrowser and licence-key parts superseded 2026-10-14 (ShardX switch, top entry).


Decided by the user: state field for fine-grained tokens, automatic sync after a
token change, real-token trial after this step. The rest are the recommendations.

- A Secrets card in the Settings tab with its own buttons, independent of Save
  Settings. Status is fetched from `GET /api/ui/secrets`, never rendered into
  the page. Errors show inline, not in `alert()`. The typed value is kept on a
  failed write except 409, and cleared when leaving the tab.
- The licence key has no field until it reaches the browser (plan section 8,
  step 5). A "Set" badge for a key that does nothing would mislead.
- `tools_status` gains `github_token_permissions` (`none`, `present`,
  `unverifiable`, `unchecked`, null when no token) and `server_time`.
  `unverifiable` also covers GitHub App tokens, so the wording is generic.
  Not verified against a real token.
- Saving or clearing the token queues one forced sync through the scheduler, as
  `trigger_build` does. The answer carries `X-Sync`: `queued`,
  `already-running`, `not-needed` or `unavailable`; the card's message follows
  it. A header, not a body key, so the slug-keyed body is unchanged. A sync that
  is already running is not re-run.
- The Scraper pill is amber for starting and stopping. A task running over 10
  minutes shows a note. The Settings tab shows a dot for a rejected token (red)
  or a warning (amber).
- `refreshDiscovery` and `checkToolsStatus` check `response.ok`; before, a
  refused sync left the Discovery list undefined.
- At the time, left for later: Apprise URLs as secrets (still rendered into the page), moving
  `sync_discovery` to the scheduler (a skipped background task can leave its
  claim held), a follow-up sync when one is already running.

## 2026-10-08: Pass cloakbrowser key explicitly and downgrade only code 77

> Superseded 2026-10-14: cloakbrowser was replaced by ShardX; see the entry at the top.


- The downloader passes a configured effective key as `launch(license_key=...)`
  and omits that argument when unset. Cloakbrowser 0.5.12 maps exit code 77 to
  its invalid/expired/missing-key message; only that specific error retries
  keyless. Seat exhaustion (76), verification failure (78), and local
  configuration failure (79) remain launch failures.
- The scraper worker compares the effective key before each task, closes the
  old downloader and launches with the new key before dispatch. Browser status
  exposes only `license_key_rejected`, never the key. A late code-77 denial
  closes the browser and fails that task; later tasks launch keyless until the
  effective configured key changes.
- Code-77 denials encountered by `search()` or `download_file()` are marked on
  the downloader and re-raised through their ordinary error handling; version
  lookup and pipeline catches also re-raise code 77 so `ScraperWorker` can close
  the browser and fail the task. The release/variant/download selectors,
  static-download fallback, `extract_app_metadata`, and build task boundary also
  preserve the denial. The version-refresh task retains its existing degraded
  error response after the worker closes the browser. Failed keyless relaunches
  retain rejection status through cooldown. Mocked regression tests cover these
  paths; no live browser validation was performed.
- At process startup, `CLOAKBROWSER_CACHE_DIR` is set to the app-owned
  `DATA_DIR/bin/cloakbrowser` cache. This prevents cloakbrowser from finding a
  key in the operator's default `~/.cloakbrowser/license.key`; startup and each
  launch refuse an unmanaged `license.key` in the app cache without reading it.
  This bypasses the Docker build-time default cache and can require a fresh
  binary download. The existing app cache must be cleared manually if it
  contains that file. Keyless launch also fails closed if a
  `CLOAKBROWSER_LICENSE_KEY` environment value reappears after startup.
- No licence UI until integration is usable. Mocked tests only; no real key or
  browser validation has been performed.
- Final corrective validation: `uv run pytest -q` passed 1528 tests with 1
  expected failure; focused Ruff, formatting and diff checks passed. Full
  `mypy src tests scripts` reports 9 pre-existing issues outside the new licence
  test (missing YAML/APScheduler stubs, one annotation, and one test request
  type mismatch). No type errors remain in `tests/test_cloakbrowser_license.py`.
  Independent static security review is complete; live browser/key validation is
  still outstanding.

## 2026-10-10: Operator documentation for secrets and request trust

- Document deployment through runtime environment variables without baking
  values into the image or examples. `DATA_DIR/.env` is described as plaintext,
  mode `0600`, and must not be sourced or loaded as a process environment file.
- Document no application authentication, trusted-network/TLS requirements,
  `ALLOWED_HOSTS` bare-host versus full-origin behavior, and preserving Host at
  the reverse proxy. Refresh root/backend maps for `.env`, the app-owned browser
  cache and request/secret modules.
- State the implemented token limitations, no licence-key UI, and key-77-only
  fallback/task recovery; do not imply live browser validation.
- Cross-check documentation examples and local links against the implementation;
  Docker command syntax and `git diff --check` pass. This was documentation-only,
  so no test suite was run.

## 2026-10-10: Cloakbrowser licence-key Settings field

> Superseded 2026-10-14: cloakbrowser was replaced by ShardX; see the entry at the top.


- Add the existing `cloakbrowser-license-key` secret to the generic Secrets card;
  keep environment ownership, invalid-environment state, password input, and
  inline failure behavior shared with the GitHub-token field.
- A saved value means stored/configured, not validated or currently active. Show
  `browser.license_key_rejected` only as the distinct last browser result. The
  worker applies changed keys before its next scraper task; saving does not
  trigger a browser call, build, or GitHub sync. Only GitHub-token writes use
  `X-Sync`.
- Keep API routes and response shapes unchanged; secret values remain absent
  from rendered HTML and status responses.

## 2026-10-10: Per-source selection API and UI contract

- Keep the default patch GET and legacy flat-list POST behavior; grouped GET is
  opt-in. New UI saves use repo-keyed mappings as full replacements. Omitted
  exclusions remain preserved by the backend.
- Repo is source identity. Legacy duplicate names resolve to the first active
  provider in config order; mapped selections distinguish same-name patches.
- Retain and display inactive/deleted selections until explicitly removed.
  Builds warn and skip unavailable mappings, and can fail if no applicable
  source remains. A partial catalog reports source errors rather than hiding
  healthy groups.
- Step 3 UI request-ownership fix is documented as implementation, not a new
  API contract or architecture decision. Review findings were resolved. The
  recorded full-suite result is 1684 passed, 1 xfailed; real CLI, production,
  and deployment E2E remain unverified.

## 2026-10-14: Uptodown as second download source

- The Morphe API sometimes resolves a version to an Uptodown page instead of
  APKMirror. The resolver now returns a typed result and accepts https URLs on
  `uptodown.com` or its subdomains, with no port or credentials; the hop limit is
  unchanged. Order: APKMirror, the resolved Uptodown page, then APKPure.
- Uptodown downloads only when the page states exactly the requested version; there
  is no "latest" substitution. The file must come from `uptodown.com`/`.net` over
  https, be `.apk`/`.xapk`/`.apks` and be a zip. Bundles go through the existing
  APKEditor merge, and the merged APK's package id must equal the requested id.
  The same package-id check now applies to files fetched through `apkeep` (APKPure).
- Trade-off: Uptodown is an unsigned third-party mirror. Nothing in the pipeline
  verifies the APK signing certificate (APKMirror copies are not verified either), so
  a compromised mirror that keeps the package id would be accepted. Accepted because
  the alternative is a failed build for those apps. Revisit with certificate
  pinning if this matters.

## 2026-10-14: APKPure URLs recognised, APKCombo left out, search results verified

- The resolver accepts https `apkpure.com`/`apkpure.net` (and subdomains, no port or
  credentials) as `VariantSource.APKPURE`, same strictness as Uptodown. No such URL
  was observed. Direct requests (redirects not followed) show the API acts as a
  per-version database: known versions redirect straight to an APKMirror or Uptodown
  page, unknown ones to `/v2/web-search-fallback/` and then a Google search, so a
  fallback means a miss. Path shapes of APKPure URLs are unknown and nothing is
  parsed from the URL. The label
  does not gate anything; `apkeep` keeps trying APKPure by package and version.
- APKCombo is not implemented: `apkcombo.com` and `apkcombo.app` return NXDOMAIN from
  public resolvers, so no page, challenge or file host could be studied. The resolver
  rejects `apkcombo.*` URLs like any other foreign host. Revisit if the domain returns.
- `ApkMirror_Downloader.search` now returns a page only if its own package id equals the
  requested one and, when a version is requested, its version equals it exactly. Before,
  a fallback search hit for another app (CapCut for a Flashscore build) supplied the
  icon, and a row title only had to contain the version text ("1.2.3" in "1.2.34").
  Trade-off: a page whose package id cannot be read is now rejected too.
