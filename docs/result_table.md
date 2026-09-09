# ResultTable system: overview and usage

This document explains the `ResultTable` system used across the CLI: how tables
are built, how plugins integrate with them, and how `@N` selection, row replay,
and plugin selectors work.

## TL;DR
- `ResultTable` is the unified object used to render tabular results and drive `@N` behavior.
- Plugins return `SearchResult` objects or dicts and can either supply row selection args or implement a `selector()` method.
- Rows can also carry an explicit `selection_action`, which the CLI replays verbatim.
- Table metadata helps plugins attach context. Some metadata keys still use older `provider` names internally.

---

## Key concepts

### ResultTable
`SYS/result_table.py` renders rows as a rich table and stores metadata used for
selection expansion.

Important APIs:
- `add_result()`
- `set_table()`
- `set_source_command()`
- `set_row_selection_args()`
- `set_row_selection_action()`
- `set_table_metadata()`

### ResultRow
A row holds columns, payload data, selection args, and optionally a full
`selection_action` token list. When `selection_action` is present, `@N` uses it
instead of reconstructing a command from the source command and row args.

### Plugin selector
If a plugin implements `selector(selected_items, ctx=..., stage_is_last=True)`,
that selector runs first when `@N` is used. If the selector returns `True`, it
has handled the selection, for example by publishing a nested table.

### Table metadata
`ResultTable.set_table_metadata(dict)` attaches plugin-specific context for
selectors and follow-up stages. You will still see legacy keys such as
`provider` in some metadata because parts of the runtime still consume them.

---

## Building a table

Typical plugin flow:

```py
from SYS.result_table import ResultTable

table = ResultTable("Plugin: X results").set_preserve_order(True)
table.set_table("plugin_name")
table.set_table_metadata({"plugin": "plugin_name", "view": "folders"})
table.set_source_command("search-file", ["-plugin", "plugin_name", "query"])

for result in results:
  table.add_result(result)

ctx.set_last_result_table(table, payloads)
ctx.set_current_stage_table(table)
```

Notes:
- To drive a direct replay, call `table.set_row_selection_args(row_index, ["-open", "<id>"])`.
- For drill-in or interactive behavior, implement `plugin.selector()` and return `True` when handled.
- For exact row replay, call `table.set_row_selection_action(row_index, tokens)`.

---

## Selection flow

When a user enters `@N`:

1. The CLI chooses the active table.
2. It gathers the selected payloads.
3. `PipelineExecutor._maybe_run_class_selector()` runs plugin `selector()` hooks for the plugin inferred from the table or payloads.
4. If no selector handles the row, the CLI checks `row.selection_action` first.
5. If no explicit row action exists, the CLI expands `source_command + source_args + row_selection_args`.
6. Multi-selection results are piped downstream instead of being collapsed to one row replay.

---

## Columns and display
- Plugins can pass a `columns` list in the result dict or `SearchResult` to control column order and display.
- Otherwise, `ResultTable` uses sensible defaults.
- Rendering helpers such as `to_rich`, `format_json`, and `format_compact` support different CLI display paths.

---

## Plugin-specific examples

### AllDebrid

AllDebrid exposes a list of magnets as folder rows and the files inside each
magnet as file rows. The plugin uses `selector()` to drill into a magnet and
publish a new `ResultTable` of files.

Example commands:

```powershell
search-file -plugin alldebrid "*"
@3
```

Illustrative magnet row:

```py
SearchResult(
  table="alldebrid",
  title="My Magnet Title",
  path="alldebrid:magnet:123",
  media_kind="folder",
  full_metadata={
    "magnet_id": 123,
    "plugin": "alldebrid",
    "plugin_view": "folders",
  },
)
```

Illustrative file row:

```py
SearchResult(
  table="alldebrid",
  title="Episode 01.mkv",
  path="https://.../unlocked_direct_url",
  media_kind="file",
  full_metadata={
    "magnet_id": 123,
    "plugin": "alldebrid",
    "plugin_view": "files",
  },
)
```

Selection and download behavior:
- `@3` on a magnet row runs the plugin selector and shows a file table.
- `@2 | download-file` downloads the selected file row.
- `@1 | add-file -plugin local -instance <dest>` triggers add-file's plugin-aware handoff flow.

Configure the AllDebrid plugin in `.config plugins` and add its API key before
testing these flows.

### Bandcamp

Bandcamp search supports `artist:` queries. The Bandcamp plugin implements a
`selector()` that detects artist rows and expands them into a discography table.

Example:

```powershell
search-file -plugin bandcamp "artist:radiohead"
@1
```

Plugin selectors are the right tool when you need to replace one table with
another, such as artist to discography drill-in.

---

## Plugin author checklist
- Implement `search(query, limit, filters)` and return `SearchResult` objects or dicts.
- Include useful `full_metadata` values for drill-in and selection replay.
- Implement `download(result, output_dir)` when the plugin can materialize file bytes.
- Implement `selector(...)` for drill-in or interactive transforms.
- Add tests that cover search, select, and download flows.

---

## Debugging tips
- Use `ctx.set_last_result_table(table, payloads)` to publish a table while developing a selector.
- Add `log(...)` messages in plugin code to capture fail points.
- Inspect `full_metadata` when a selector or replay path is missing required context.

---

## Quick reference
- ResultTable implementation: `SYS/result_table.py`
- Pipeline helpers: `SYS/pipeline.py`
- Row replay and selection flow: `SYS/pipeline.py`
- Plugin authoring guide: `docs/plugin_authoring.md`
