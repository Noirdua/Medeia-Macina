# Plugin Development Guide

## Purpose
This guide describes how to write, test, and register a plugin so the
application can discover and use it as a pluggable component.

The public model is plugin-first. The internal base class is `Plugin`
(`Provider` remains an alias).

Keep plugin code small, focused, and well-tested. Bundled plugins and drop-in
plugins share the same `plugins/` layout.

---

## Anatomy of a plugin
A plugin is a Python class that extends `PluginCore.base.Plugin` and implements
a few key methods and attributes.

Minimum expectations:
- `class MyPlugin(Plugin):` subclasses the base plugin class.
- `PLUGIN_NAME`, `PLUGIN_VERSION`, `PLUGIN_AUTHOR`, `PLUGIN_DESCRIPTION` identify the plugin in `.plugin` / `.plugin -available`.
- `PLUGIN_REQUIRES` and/or `requirements.txt`: pip packages installed by `.plugin -add` / `-update`.
- `PLUGIN_DEPENDS`: other plugin names required (e.g. `("playwright",)`). `.plugin -install` asks to install them first.
- `URL`, `URL_DOMAINS`, or `url_patterns()` let the registry route URLs.
- `validate(self) -> bool` returns `True` when the plugin is configured and usable.
- `search(self, query, limit=50, filters=None, **kwargs)` returns a list of `SearchResult` items.

Optional but common:
- `download(self, result: SearchResult, output_dir: Path) -> Optional[Path]`
- `QUERY_ARG_CHOICES`: `-query` fields for completer (`{"artist": (), "album": ("lp", "ep")}`). Empty tuples still offer `artist:`.
- `selector(self, selected_items, *, ctx, stage_is_last=True, **kwargs) -> bool`
- `download_url(self, url, output_dir, progress_cb=None)`
- `CONFIG_HELP`: string or lines shown in `.config` for this plugin (how to get an API key, etc.)
- Schema field `"help"`: extra line for that setting in `.config`

---

## SearchResult
Use `PluginCore.base.SearchResult` to describe results returned by `search()`.

Important fields:
- `table` (str): plugin table name
- `title` (str): short human title
- `path` (str): canonical URL or link the plugin or downloader may use
- `media_kind` (str): `file`, `folder`, `book`, and similar values
- `columns` (list[tuple[str, str]]): extra key/value pairs to display
- `full_metadata` (dict): plugin-specific metadata for downstream stages
- `annotations` or `tag`: simple metadata for filtering

Return a list of `SearchResult(...)` objects or simple dicts convertible with `.to_dict()`.

---

## Implementing `search()`
- Parse and sanitize `query` and `filters`.
- Return no more than `limit` results.
- Use `columns` to provide table columns such as `TITLE`, `Seeds`, or `Size`.
- Keep `search()` fast and predictable by using reasonable timeouts.

Example:

```python
from PluginCore.base import Plugin, SearchResult


class HelloPlugin(Plugin):
    def search(self, query, limit=50, filters=None, **kwargs):
        q = (query or "").strip()
        if not q:
            return []
        results = [
            SearchResult(
                table="hello",
                title=f"Hit for {q}",
                path=f"https://example/{q}",
                columns=[("Info", "example")],
                full_metadata={"source": "hello"},
            )
        ]
        return results[:max(0, int(limit))]
```

---

## Implementing `download()` and `download_url()`
- Prefer plugin `download(self, result, output_dir)` for piped plugin items.
- For plugin-provided URLs, implement `download_url` so `download-file` can route downloads through the plugin.
- Use the repo `_download_direct_file` helper for HTTP downloads when possible.

Example download method:

```python
def download(self, result: SearchResult, output_dir: Path) -> Optional[Path]:
    url = getattr(result, "path", None)
    if not url or not url.startswith("http"):
        return None
    return _download_direct_file(url, output_dir)
```

---

## URL routing
Plugins can declare:
- `URL = ("magnet:",)` or similar prefix lists
- `URL_DOMAINS = ("example.com",)` to match hosts
- `@classmethod def url_patterns(cls):` to combine static and dynamic patterns

The registry uses these declarations to match `download-file <url>` and to pick
which plugin should handle a URL.

---

## Selector behavior and `@N`
- Implement `selector(self, selected_items, *, ctx, stage_is_last=True)` to present a sub-table or enqueue downloads.
- Use `ctx.set_last_result_table()` and `ctx.set_current_stage_table()` to display follow-up tables.
- Return `True` when the selector handled the selection and the pipeline should stop expanding that row.

---

## Testing plugins
- Keep tests small and local.
- Create `tests/test_plugin_<name>.py` or follow the existing repo naming when extending older tests.
- Test `search()` with mock HTTP responses.
- Test `download()` using a temp directory and a small file server or by mocking `_download_direct_file`.
- Test `selector()` by constructing a fake result and `ctx` object.

Example PowerShell commands from the repo root:

```powershell
pytest tests/test_plugin_hello.py -q
pytest -q
```

---

## Registration and packaging
- Bundled plugins live under `plugins/` and are auto-discovered from that package.
- External plugins can be dropped into `plugins/` or any directory listed in `MM_PLUGIN_PATH` or `MEDEIA_PLUGIN_PATH`.
- Package directories are preferred so plugin-specific files travel with the plugin.
- Plugin authors should import from `PluginCore.*`.

If a plugin supports multiple configured endpoints or accounts, the user-facing
concept is a plugin instance. Config lives under `plugin.<plugin>.<instance>`.

---

## Shared helpers
Do not copy filename, path, config, or size helpers into a plugin. Import them:

- `SYS.utils.sanitize_filename` — filesystem-safe names (Windows reserved names, illegal chars, length)
- `SYS.utils.unique_path` — append ` (n)` when a file already exists
- `SYS.utils.coerce_bool` / `coerce_int`
- `SYS.utils.format_bytes` (`format_byte_size` is an alias)
- `SYS.utils.sha256_file`, `ensure_directory`, `safe_output_dir`, `default_staging_dir`
- `API.HTTP.HTTPClient` and `download_direct_file` for HTTP

## Best practices
- Use `debug()` and `log()` appropriately; avoid noisy stderr output in normal runs.
- Prefer returning `SearchResult` objects to provide consistent UX.
- Keep `search()` tolerant of timeouts and malformed responses.
- Use `full_metadata` to pass non-display data to `download()` and `selector()`.
- Respect the `limit` parameter in `search()`.
- Call the shared helpers above instead of local `_safe_filename` / `_unique_path` copies.

---

## Validate a plugin
From the repo root:

```powershell
python -m PluginCore.validate
python -m PluginCore.validate hello
python -m PluginCore.validate path\to\myplugin
```

In the REPL: `.plugin` or `.plugin hello`.

Install from the plugin source repository (not a git submodule). Default source is
`plugin_source` in `.config` (overridable with `MM_PLUGIN_SOURCE`):

```
.plugin -source
.plugin -source /path/to/Medeia-Macina-Plugin
.plugin -available
.plugin -add hydrusnetwork
.plugin -update
```

`plugin_source` may be a git URL or a local folder. A local DLC checkout (for
example a sibling `Medeia-Macina-Plugin` folder) is preferred while developing: edit
plugins there, then `.plugin -update` copies them into this app’s `plugins/`.
Do not treat the app `plugins/` tree as the source of truth.

Remote sources are cloned into `.plugin-source/`. Put each DLC plugin in its own
folder with `__init__.py` (optionally under a top-level `plugins/` directory).

To uninstall (config leftovers after deleting a plugin folder):

```
.plugin -remove hydrusnetwork
.plugin -remove -orphans
.plugin -remove hello -files
```

`-orphans` drops config for plugins that are no longer installed. `-files` also deletes the plugin folder.

The checker confirms folder layout (`plugins/<name>/__init__.py`), a `Plugin` subclass, `PLUGIN_NAME`, `SUPPORTED_CMDLETS` vs implemented methods, `config_schema()`, and that the class can be constructed. Support packages without a `Plugin` class (Playwright, MPV helpers) are reported as `support`, not failures.

## Plugin commands and help
Extra commands (not `file -search` / `file -download`) live in `commands.py` next
to `__init__.py`. `register_plugin_commands` loads `plugins.<name>.commands`.

Each command must be a `Cmdlet` with `summary`, `usage`, `examples`, and `detail`
so `.help unlock-link` works. Bare `@register(["unlock-link"])` functions have no
help.

```python
from SYS.cmdlet_spec import Cmdlet, CmdletArg

CMDLET = Cmdlet(
    name="unlock-link",
    summary="Unlock a hoster link through AllDebrid",
    usage="unlock-link <url>",
    examples=["unlock-link https://hoster.example/file", "@1 | unlock-link"],
    detail=["Requires AllDebrid API Key in .config."],
    arg=[CmdletArg("url", type="string", description="Restricted URL")],
)
CMDLET.exec = _run
COMMANDS = [CMDLET]
```

`python -m PluginCore.validate` warns when `summary`, `usage`, or `examples` are missing.

Extra `file -<action>` flags use `FILE_ACTIONS` on the Plugin class (see playwright). Extra `metadata -<action>` flags use `METADATA_ACTIONS` (see metadata+).

## Example plugin checklist
- [ ] Put the plugin in `plugins/<name>/__init__.py`.
- [ ] Subclass `Plugin` and set `PLUGIN_NAME` to the folder name.
- [ ] Set `PLUGIN_VERSION`, `PLUGIN_AUTHOR`, `PLUGIN_DESCRIPTION`.
- [ ] Set `SUPPORTED_CMDLETS` to the cmdlets you actually implement.
- [ ] Implement `search()` / `download()` / `upload()` to match those cmdlets.
- [ ] Implement `validate()` for required config.
- [ ] Set `CONFIG_HELP` (and schema `"help"`) so `.config` explains how to get keys.
- [ ] Extra commands go in `commands.py` with summary, usage, examples, detail.
- [ ] Provide `URL`, `URL_DOMAINS`, or `url_patterns()` if you handle URLs.
- [ ] Run `python -m PluginCore.validate <name>` until it PASSes.

---

## Further reading
- See existing bundled plugins in `plugins/` for patterns and edge cases.
- Check `API/` helpers for HTTP and debrid clients used by plugins.
