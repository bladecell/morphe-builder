# src/templates/

## Responsibility

Jinja2 views rendered by `src/main.py`: the single-page admin UI and the page Obtainium
scrapes.

## Design

**`index.html`** is one file. Its stylesheet (`src/static/app.css`, built from the
templates by `scripts/build_css.py`) and Alpine.js (vendored in `src/static/`) are
served by the app; nothing loads from a CDN. Rebuild the stylesheet after changing
classes here.
All client state and behaviour live in a single Alpine component,
`Alpine.data('morphe', ...)`, bound on `<body x-data="morphe">`.

Rendering is hybrid:

- Server side, Jinja injects initial state as JSON (`available_apps`, `config.apps`,
  `config.sources`, `config.settings`, `metadata`) and renders the pipeline cards with
  `{% for app in config.apps %}`.
- Client side, Alpine renders everything else and polls for live state.

Because the pipeline cards are server-rendered, adding, editing or deleting an app, or
adding a source, ends with `location.reload()`. The active tab survives a reload via
`sessionStorage.activeTab`.
Build, source, app, patch-save and notification actions report request failures inline;
source/app deletion still asks for confirmation before submitting.

Three tabs, switched by `activeTab`: `discovery`, `pipeline` (default), `settings`.
Two modals: patch configuration (`showModal`) and manual APK upload
(`showUploadModal`).

Patch selection in the configuration dialog uses the opt-in grouped catalog from
`GET /api/ui/patches/{pkg}?format=grouped`. Source identity is the repository, while
the display name is descriptive only. The backend supplies legacy-list resolution and
stored/unresolved selection state; the UI converts to a repo-keyed mapping only on an
explicit save. Unavailable mapped selections remain selected until explicitly removed.
Mapping saves are full replacements; the modal round-trips inactive/deleted entries and
omits exclusions so the backend preserves them.

**`obtainium.html`** is a static card. Its contract with Obtainium's HTML source is one
anchor whose `href` is the APK download URL and whose text ends in `.apk`, plus the
version in the page text.

## Flow

Polling started in `init()`:

| Interval | Calls |
|---|---|
| 2 s | `checkBuildStatus`; `fetchLogs` while building |
| 10 s | `checkToolsStatus`; `checkUpdates` when not building |

When a build finishes, `checkBuildStatus` triggers `checkUpdates` and `refreshMetadata`.
When a tool sync finishes, `checkToolsStatus` triggers `refreshDiscovery`. It also
copies `sync_error` into `syncError`, shown as a red notice above the source list.
`fetchLogs` keeps only the text after the last `\r` on each line, collapsing progress
bars.

## Integration

Endpoints called, by area:

| Area | Method -> endpoint |
|---|---|
| Discovery tab | `addSource` -> `POST /api/ui/add_source`; `toggleSource` -> `toggle_source`; `deleteSource` -> `delete_source`; `refreshDiscovery` -> `GET /api/ui/discovery[?sync=true]` |
| Patch modal | `openAddModal`, `openEditModal` -> grouped `GET /api/ui/patches/{pkg}?format=grouped`; `saveApp` -> `POST /api/ui/add_app` with a full source-keyed `include_patches` replacement. The existing app exclusions remain omitted so the backend preserves them. |
| Pipeline tab | `triggerBuild` -> `POST /api/ui/trigger_build`; inline delete -> `POST /api/ui/delete_app`; `uploadApk` -> `POST /api/ui/upload_apk` (multipart); `checkUpdates` -> `GET /api/ui/updates`; `fetchLogs` -> `GET /api/ui/logs`; icons from `/icons/{pkg}.png` |
| Header and status | `checkBuildStatus` -> `GET /api/ui/build_status`; `checkToolsStatus` -> `GET /api/ui/tools_status`; `refreshMetadata` -> `GET /api/ui/metadata` |
| Settings tab | `saveSettings` -> `POST /api/ui/save_settings`; notification destinations use `GET /api/ui/secrets`, `PUT`/`DELETE /api/ui/secrets/apprise-urls`, explicit `POST /api/ui/secrets/apprise-urls/migration`, and bodyless `POST /api/ui/test_notification`. Secret status reports destination count/source only; values are write-only. The card keeps URL drafts client-side until save and never renders stored URLs. GitHub-token writes alone use `X-Sync`. |
| Obtainium | `addToObtainium` opens `obtainium://add/<server_url>/view/{id}`; card link opens `/view/{id}` |

`sourceUpdates` is read by the source list but never populated; the route it
named, `GET /api/ui/source_updates`, was deleted.

`obtainium.html` is rendered by `main.view_app` with `app`, `version`, `patch_version`
and `download_url`.
