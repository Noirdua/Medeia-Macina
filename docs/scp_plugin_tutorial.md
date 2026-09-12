# SCP Plugin Walkthrough

This walkthrough covers the bundled `scp` plugin, backed by existing SSH
libraries:

- `paramiko` for SSH and SFTP directory listing
- `scp` for file transfers

The implementation lives in [plugins/scp/__init__.py](plugins/scp/__init__.py).

## What the plugin does

The SCP plugin mirrors the FTP walkthrough, but on top of SSH:
- `search-file -plugin scp -instance <name> ...` lists remote files and folders over SFTP
- plain `@N` on a folder drills into that directory
- plain `@N` on a file runs `download-file -plugin scp -instance <name> -url ...`
- `@N | add-file -instance ...` downloads first, then ingests the local temp file
- `add-file <local-file> -plugin scp -instance <name>` uploads a local file to the configured remote path

## Example config

Add one or more named SCP plugin instances to your config under `plugin.scp.<instance>`:

```toml
[plugin.scp.work]
host = "ssh.example.com"
port = 22
username = "deploy"
password = "secret"
key_path = "~/.ssh/id_ed25519"
base_path = "/srv/files"
timeout = 20
search_depth = 1
allow_agent = true
look_for_keys = true

[plugin.scp.archive]
host = "ssh-archive.example.com"
port = 2222
username = "archive"
key_path = "~/.ssh/id_ed25519"
base_path = "/srv/archive"
timeout = 20
```

Notes:
- `work` and `archive` are instance names.
- `host` and `username` are required for each instance to validate.
- You can use password auth, key auth, or both.
- `base_path` is both the default search root and the default upload directory.
- You can browse configured instances from `.config plugins` in the CLI.

## Search flow

```powershell
search-file -plugin scp -instance work "*"
search-file -plugin scp -instance work "invoice"
search-file -plugin scp -instance work "path:/srv/files/releases depth:2 *.zip"
search-file -plugin scp -instance work "path:/srv/files type:folder *"
```

## Selection flow

Folder rows are navigation rows:

```powershell
search-file -plugin scp -instance work "*"
@2
```

File rows carry an explicit row action, so terminal selection downloads
directly:

```powershell
search-file -plugin scp -instance work "report"
@1
```

That expands to the equivalent of:

```powershell
download-file -plugin scp -instance work -url scp://ssh.example.com/srv/files/report.pdf
```

## Download and add-file flow

```powershell
search-file -plugin scp -instance work "report"
@1 | download-file -path C:\Downloads

search-file -plugin scp -instance work "report"
@1 | add-file -instance tutorial
```

Why this works:
- file rows advertise `_selection_action` for `download-file`
- `add-file` selection replay inserts that plugin download stage before ingest
- the plugin also implements `resolve_pipe_result_download()` for plugin-owned SCP rows
- file rows carry the chosen `instance`, so replay stays bound to the same SSH target

## Upload flow

```powershell
add-file C:\Media\report.pdf -plugin scp -instance archive
```

## Implementation notes

The plugin uses SFTP for directory listing because SCP itself is a transfer
protocol, not a browse/search protocol. That split keeps the plugin simple:
- browse and metadata via Paramiko SFTP
- file transfer via the `scp` package

## Recommended demo commands

```powershell
search-file -plugin scp -instance work "*"
search-file -plugin scp -instance work "path:/srv/files depth:2 *.zip"
@1
@1 | download-file -path C:\Downloads
@1 | add-file -instance tutorial
add-file C:\Media\report.pdf -plugin scp -instance archive
```