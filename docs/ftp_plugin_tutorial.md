# FTP Plugin Walkthrough

This walkthrough covers the bundled `ftp` plugin. It lets users:

- run `search-file -plugin ftp -instance <name> ...`
- browse remote folders as result tables
- select file rows to `download-file`
- pipe selected file rows into `add-file`
- upload local files with `add-file -plugin ftp -instance <name>`

The implementation lives in [plugins/ftp/__init__.py](plugins/ftp/__init__.py).

## What the plugin does

The FTP plugin demonstrates the main plugin hooks that matter for a
storage-style integration:

- `config_schema()` exposes host, credentials, base path, TLS, and search depth.
- `extract_query_arguments()` supports inline query fields like `path:` and `depth:`.
- `search()` walks an FTP directory tree and returns `SearchResult` rows.
- `selector()` turns folder rows into a follow-up table when the user runs `@N`.
- `download()` and `download_url()` fetch FTP files into `download-file` output paths.
- `resolve_pipe_result_download()` lets `@N | add-file -instance ...` materialize a remote FTP file first.
- `upload()` lets `add-file <local-file> -plugin ftp -instance <name>` push a local file to the configured FTP server.

## Example config

Add one or more named FTP plugin instances to your config under `plugin.ftp.<instance>`:

```toml
[plugin.ftp.work]
host = "ftp.example.com"
port = 21
username = "demo"
password = "secret"
base_path = "/incoming"
tls = false
passive = true
timeout = 20
search_depth = 1

[plugin.ftp.archive]
host = "archive.example.com"
port = 2121
username = "archive-bot"
password = "secret"
base_path = "/dropbox"
tls = true
```

Notes:
- `work` and `archive` are instance names.
- `host` is the only required field for each instance to validate.
- `username` defaults to `anonymous` and `password` defaults to `anonymous@`.
- `base_path` is both the default search root and the upload target directory.
- `search_depth` controls how many folder levels `search-file -plugin ftp` scans by default.
- You can browse configured instances from `.config plugins` in the CLI.

## Search flow

```powershell
search-file -plugin ftp -instance work "*"
search-file -plugin ftp -instance work "invoice"
search-file -plugin ftp -instance work "path:/pub depth:2 invoice"
search-file -plugin ftp -instance work "path:/pub type:folder *"
```

The plugin returns rows with explicit columns for name, type, directory, size,
and modification time.

## Selection flow

Folder rows are navigation rows:

```powershell
search-file -plugin ftp -instance work "*"
@2
```

File rows carry an explicit row action equivalent to:

```powershell
download-file -plugin ftp -instance work -url ftp://ftp.example.com/incoming/report.pdf
```

So plain `@N` on a file row downloads it immediately:

```powershell
search-file -plugin ftp -instance work "report"
@1
```

## Download and add-file flow

```powershell
search-file -plugin ftp -instance work "report"
@1 | download-file -path C:\Downloads

search-file -plugin ftp -instance work "report"
@1 | add-file -instance tutorial
```

Why this works:
- the file row advertises a `download-file` row action
- the pipeline auto-inserts that download before `add-file`
- the FTP plugin also implements `resolve_pipe_result_download()` so plugin-owned FTP rows can be materialized for ingestion
- file rows carry the chosen `instance`, so selection replay and `@N | add-file ...` keep the same FTP target

## Upload flow

Uploading uses the same plugin, through `add-file -plugin ftp -instance <name>`:

```powershell
add-file C:\Media\report.pdf -plugin ftp -instance archive
```

That sends the file to the selected instance's FTP `base_path` and returns the
FTP URL as the uploaded result.

## Why the row metadata matters

The critical part of this plugin is the file-row metadata:
- file rows emit `_selection_args` as `['-instance', '<name>', '-url', '<ftp-url>']`
- file rows emit `_selection_action` as `['download-file', '-plugin', 'ftp', '-instance', '<name>', '-url', '<ftp-url>']`
- folder rows do not emit a download action, so `selector()` owns drill-in behavior instead

That keeps these flows compatible:
- `@N` on a folder opens a new table
- `@N` on a file downloads the file
- `@N | add-file -instance ...` first downloads, then ingests

## Implementation notes

The plugin prefers `MLSD` for directory listings and falls back to `NLST` plus
directory probes when the server does not support machine-readable listings.

The code intentionally stays small and uses only Python stdlib pieces:
- `ftplib` for FTP and FTPS
- `fnmatch` for wildcard-style search tokens
- `tempfile` for `add-file` handoff downloads

## Recommended demo commands

```powershell
search-file -plugin ftp -instance work "*"
search-file -plugin ftp -instance work "path:/incoming depth:2 *.pdf"
@1
@1 | download-file -path C:\Downloads
@1 | add-file -instance tutorial
add-file C:\Media\report.pdf -plugin ftp -instance archive
```