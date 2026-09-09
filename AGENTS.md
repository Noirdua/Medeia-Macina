# Agent notes — Medeia-Macina

This file is for humans and coding agents picking up the repo. Prefer it over guessing layout, dual APIs, or “second versions” of features.

## What this is

Medeia-Macina (`mm` / `medeia`) is a **text-first media CLI**: search, download, tag, archive, play, and pipe results between commands. The UX is **plugin-first** and **table-first**: commands emit result tables; `@N` replays a row; `|` pipes into the next cmdlet.

Public name: **plugin**. Internal Python type is `PluginCore.base.Plugin` (`Provider` is an alias). Config is stored under `plugin.*` (legacy `tool` / `provider` / `store` keys are migrated on load).

Python: **3.10–3.13** (`pyproject.toml` says `>=3.10,<3.15`). Installer: `scripts/bootstrap.py`. Console entry: `scripts.cli_entry:main` → `CLI.MedeiaCLI`.

Do **not** invent URLs. Use URLs from the user, local files, or this repo’s docs. Do **not** commit unless explicitly asked.

## Layout

Startup chrome is loadable from `design/`: `mm.md` (`[mm]` launcher prefix), `kappa.md` (DELTA/KAPPA/LAMBDA banner + YAML frontmatter), `quote.md` (exit line).


| Path | Role |
|---|---|
| `CLI.py` | REPL, pipeline parsing, `@N` selection, `.config` |
| `SYS/cli_completer.py` | Prompt-toolkit cmdlet/plugin/instance completion |
| `scripts/cli_entry.py` | `mm` / `medeia` launcher; puts repo root on `sys.path` |
| `scripts/bootstrap.py` | Canonical installer (venv, deps, Hydrus extra, systemd service) |
| `scripts/run_client.py` | Hydrus **headless launcher** + service install helpers |
| `scripts/hydrusnetwork.py` | Hydrus **clone/venv/deps**; imports helpers from `run_client.py` |
| `cmdlet/` | User commands (`file -search`, `file -add`, metadata cmdlets) |
| `cmdlet/file/` | File actions split by concern (`search.py`, `download_core.py`, `add_*.py`) |
| `cmdnat/` | Native/internal commands (`.config`, status); `_parsing.py` re-exports `SYS.command_parsing` |
| `PluginCore/` | Plugin load, `Provider`/`SearchResult`, `BackendRegistry` |
| `plugins/<name>/` | One plugin per folder; optional `api/`, `store_proxy.py`, `store_backend.py` |
| `SYS/` | Config, SQLite, result tables, pipeline, logging, utils |
| `API/` | HTTP: `HTTP.py` (`HTTPClient`), `httpx_shared.py` (pooled clients), `requests_client.py` |
| `tests/` | Pytest files named `test_*.py`. **Gitignored** by `.gitignore` `test*` — they exist on disk but are **not shipped** unless force-added. Run with `python -m pytest tests/... -o addopts=` to skip coverage defaults. |

## Runtime model

1. User types a line in the REPL (or `mm` non-interactive).
2. `CLI` splits on `|` into stages; `@N` / `@N-M` expands from the last result table.
3. Cmdlets are class-based (`SYS.cmdlet_spec.Cmdlet`). `cmdlet/file_cmdlet.py` is the `file` umbrella (`-search`, `-add`, `-download`, …).
4. Plugins implement `search`, `download_url` / `handle_url`, and/or storage methods. Cmdlets resolve plugins via `PluginCore.registry.get_plugin` and backends via `PluginCore.backend_registry.BackendRegistry`.
5. Results go into `SYS.result_table.Table` (Rich). Rows carry `columns`, `_selection_args`, `_selection_action`.

**There is one result-table stack:** `SYS.result_table.Table`. Do not reintroduce `result_table_api` / adapters / `plugin-table` / `ResultModel` — that second API was removed.

## Plugins vs backends

- **Plugin** (`Provider`): search/download/UI. Config: `plugin.<name>.<instance>` with `NAME`, plus plugin-specific keys (`URL`, `API`, …). Target with `-plugin` / `-instance`.
- **Backend** (`BackendBase`): storage (add/get/delete file, tags, URLs, notes). Hydrus instances are backends of `STORE_TYPE = "hydrusnetwork"`.
- Discovery: `plugins/` in repo, `plugins/` in cwd, `MM_PLUGIN_PATH`, `MEDEIA_PLUGIN_PATH`.
- **Plugin source of truth is the DLC plugin repo** (sibling folder or git URL). Edit plugins there, not copies under this repo’s `plugins/`. Install/update with `.plugin -add NAME` / `.plugin -update`. Point `.plugin -source` at that folder or a git URL (`plugin_source` in `.config`, or `MM_PLUGIN_SOURCE`).
- New plugins: folder `<dlc>/<name>/__init__.py`, `PLUGIN_NAME`, inherit `Plugin`. Package-owned HTTP belongs in `<name>/api/`.

**Hifi** is a thin subclass of **Tidal** (`plugins/hifi/__init__.py`). Tidal table names use `self.PLUGIN_NAME`. The API client class is `TidalApi` (`from plugins.tidal.api import Tidal as TidalApi`) so it does not collide with `class Tidal(Plugin)`.

**Archive.org** (`plugins/archiveorg/`, `PLUGIN_NAME = "archiveorg"`) is the merged Internet Archive + OpenLibrary plugin. They share one login. Aliases: `archive.org`, `openlibrary`, `internetarchive`, `ia`. Config keys `plugin.openlibrary` / `plugin.internetarchive` / `plugin.archive.org` migrate into `plugin.archiveorg`. `-plugin openlibrary` still runs the OpenLibrary book/borrow search; default Archive.org search is IA items. `-query "book:..."` (or `openlibrary:`) searches OpenLibrary for borrowable books. Borrow login: GET `/services/csrf-token`, then POST `/services/account/login/` with `X-Csrf-Token`.

## Hydrus (important)

Three layers — do not add a fourth:

| Layer | File | Job |
|---|---|---|
| Plugin | `plugins/hydrusnetwork/__init__.py` | Search, instance resolve, `__getattr__` forwards storage to the operations backend |
| Proxy | `plugins/hydrusnetwork/store_proxy.py` | `BackendBase` for `BackendRegistry`; lazy `_operations()` |
| Operations | `plugins/hydrusnetwork/store_backend.py` | Real API work |
| Client | `plugins/hydrusnetwork/api/__init__.py` | HTTP/CBOR to Hydrus Client API |

- **File uploads** (`add_file`) use `http.client` with explicit `Content-Length` (`_put_raw_file`), not httpx streaming. Caddy + chunked bodies made Hydrus return `Unknown filetype!`.
- Hydrus HTTP for JSON/GET uses `HTTPClient(..., trust_env=False)` so `HTTP(S)_PROXY` does not wrap API calls.
- Hydrus metadata for cmdlets: `hydrus_provider.fetch_metadata` / `get_title`. Payload-style helper in `plugins.hydrusnetwork.api` is `_fetch_hydrus_metadata_payload` (internal).
- Headless client: `QT_QPA_PLATFORM=offscreen` via `python3 run_client.py --headless`. Venv may be `.venv` **or** `venv`; `find_venv_python` checks both.
- DietPi/systemd: bootstrap installs `ffmpeg`, `libglib2.0-0`, and related Qt libs. **Missing `ffmpeg` on the Hydrus host** → MIME detection fails → `Unknown filetype!` (not a proxy bug).
- Bootstrap copies `scripts/run_client.py` into the Hydrus checkout (local copy, else download from the Forgejo raw URL). Do not depend on an untracked repo-root `run_client.py`.

## HTTP

- Prefer `API.HTTP.HTTPClient` for cmdlets/plugins. It uses `API.httpx_shared.get_shared_httpx_client` (pooled; do not close the shared client in `__exit__`).
- Page scrape / crawlers: `API.requests_client.get_requests_session`.
- Downloads: `download_direct_file` in `API.HTTP`. Unique filenames: `SYS.utils.unique_path`. Filename sanitizing: `SYS.utils.sanitize_filename`. Booleans/ints from config: `SYS.utils.coerce_bool` / `coerce_int`.
- Field access: `SYS.item_accessors.get_field`; `SYS.field_access` re-exports it.
- Arg parsing: `SYS.command_parsing`; `cmdnat._parsing` re-exports it.
- Pipeline imports: `from SYS import pipeline as ctx` is the public facade (`pipeline_state` + `PipelineExecutor`). Do not add a third pipeline module.

## Help

- `.help` lists **canonical** commands only (`file`, `metadata`, `.worker`, plugins). Aliases stay on the Aliases column.
- `.worker` / `worker` / `workers` are the same command.
- `search-file`, `add-file`, `tag`, `get-relationship`, etc. still run, but are hidden from the index because `file` / `metadata` cover them. `.help search-file` still works.
- Tag templates/regex live on `metadata -add` / `metadata -delete` (`$(ns)`, `<regex(...)>`). Prefix is config `tag_placeholder_prefix` (default `$`; `#(` still works). See `docs/tag_template_syntax.md`.

## File cmdlets worth knowing

- `file -search <url>` scrapes a page for downloadable types (`cmdlet/file/search_engines.py`: `scrape_page_assets`, `assemble_scrape_result_rows`).
- `file -download <url> -scrape` (or `-query type:image`) uses the same scrape row builder.
- `file -download` of a normal URL must not require Hydrus to be up (preflight skips dead Hydrus backends).
- `file -add -plugin hydrusnetwork -instance <name>` copies into that instance.

## Config and data

- SQLite: `medios.db` at repo/app root (`SYS.database`). Config rows in `config` table; `SYS.config.load_config` / save with a cross-process lock.
- `.config` in the REPL is the config UI, not a dotenv file.
- Debug: `MM_DEBUG=1` or config `debug=true`. HTTP traces go through `SYS.logger.debug_panel`.

## Conventions

- Match existing style; no new comments unless asked.
- Cmdlets: subclass `Cmdlet`, declare `args`, implement `_run_impl`.
- Plugins: `PLUGIN_NAME`, `SUPPORTED_CMDLETS`, `TABLE_AUTO_STAGES` for `@N` follow-ups.
- One implementation per concern. If you find a second copy (table API, HTTP wrapper, unique_path, hifi clone), delete or re-export; do not add a third.
- Tests: `python -m pytest tests/<file> -o addopts=` (default addopts enable coverage). Tests are often gitignored.

## Commands

```bash
python scripts/bootstrap.py          # install / extras / Hydrus service
mm                                   # REPL
python -m py_compile <paths>         # quick syntax check
python -m PluginCore.validate        # plugin layout/contract checks
.plugin -available / -add NAME       # install from config plugin_source (not a submodule)
.plugin -update                      # pull source (git or local DLC folder) and recopy installed plugins
python -m pytest tests/test_foo.py -o addopts=
```

Lint (if needed): `black` / `flake8` / `pylint` from `[project.optional-dependencies] dev`. There is no required lint gate in-repo.

## Pitfalls

- `.gitignore` has `test*` — new tests will not be committed unless force-added or the ignore is adjusted.
- Hydrus GETs through Caddy can succeed while large POSTs fail for other reasons (ffmpeg missing, chunked body). Read the **Hydrus process traceback**, not only the client `status=4`.
- `class Tidal` vs `plugins.tidal.api.Tidal`: always alias the API client as `TidalApi`.
- Do not reintroduce `AsyncHTTPClient` unless something actually needs async httpx.
- Windows: PowerShell 7+; quote paths with spaces.
