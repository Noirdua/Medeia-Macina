from __future__ import annotations

from typing import Any

from SYS.result_table import Table


def upper_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.upper()


def add_startup_check(
    table: Table,
    status: str,
    name: str,
    *,
    provider: str = "",
    instance: str = "",
    files: int | str | None = None,
    detail: str = "",
) -> None:
    row = table.add_row()
    row.add_column("STATUS", upper_text(status))
    row.add_column("NAME", upper_text(name))
    row.add_column("PLUGIN", upper_text(provider or ""))
    row.add_column("INSTANCE", upper_text(instance or ""))
    row.add_column("FILES", "" if files is None else str(files))
    row.add_column("DETAIL", upper_text(detail or ""))


def _provider_config_map(config: dict) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}

    provider_cfg = config.get("plugin")
    return provider_cfg if isinstance(provider_cfg, dict) else {}


def _iter_registered_plugin_infos() -> tuple[Any, ...]:
    try:
        from PluginCore.registry import REGISTRY

        return tuple(
            sorted(
                REGISTRY.iter_plugins(),
                key=lambda info: str(
                    getattr(info, "canonical_name", "") or ""
                ).lower(),
            )
        )
    except Exception:
        return ()


def _extract_configured_instance_names(raw_entry: Any) -> list[str]:
    if not isinstance(raw_entry, dict) or not raw_entry:
        return []
    if not all(isinstance(value, dict) for value in raw_entry.values()):
        return []

    names: list[str] = []
    for key in raw_entry.keys():
        name = str(key or "").strip()
        if not name or name.lower() == "default":
            continue
        names.append(name)
    return names


def _resolve_startup_instance_text(
    plugin: Any,
    summary: dict[str, Any],
    configured_entry: Any,
) -> str:
    instance_text = str(summary.get("instance") or "").strip()
    if instance_text:
        return instance_text

    raw_instances = summary.get("instances")
    if isinstance(raw_instances, (list, tuple, set)):
        values = [str(value).strip() for value in raw_instances if str(value).strip()]
        if values:
            return ", ".join(values)
    elif raw_instances is not None:
        instance_text = str(raw_instances).strip()
        if instance_text:
            return instance_text

    try:
        configured_instances = plugin.configured_instances() if plugin is not None else []
    except Exception:
        configured_instances = []

    if configured_instances:
        return ", ".join(str(value).strip() for value in configured_instances if str(value).strip())

    return ", ".join(_extract_configured_instance_names(configured_entry))


def has_provider(cfg: dict, name: str) -> bool:
    provider_cfg = cfg.get("plugin")
    if not isinstance(provider_cfg, dict):
        return False
    block = provider_cfg.get(str(name).strip().lower())
    return isinstance(block, dict) and bool(block)


def ping_url(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        from API.HTTP import HTTPClient

        with HTTPClient(timeout=timeout, retries=1) as client:
            response = client.get(url, allow_redirects=True)
        code = int(getattr(response, "status_code", 0) or 0)
        ok = 200 <= code < 500
        return ok, f"{url} (HTTP {code})"
    except Exception as exc:
        import httpx as _httpx
        if isinstance(exc, _httpx.TimeoutException):
            return False, f"{url} (timeout)"
        return False, f"{url} ({type(exc).__name__})"


def provider_display_name(key: str) -> str:
    label = (key or "").strip().lower()
    if not label:
        return "Plugin"

    # Preserve expected brand casing for common providers.
    display_overrides = {
        "youtube": "YouTube",
        "archive.org": "Archive.org",
        "archiveorg": "Archive.org",
        "openlibrary": "OpenLibrary",
        "podcastindex": "PodcastIndex",
    }
    if label in display_overrides:
        return display_overrides[label]
    return label[:1].upper() + label[1:]


def default_provider_ping_targets(key: str) -> list[str]:
    """Return default health-check URLs for known providers."""
    label = (key or "").strip().lower()
    defaults = {
        "bandcamp": ["https://bandcamp.com"],
        "youtube": ["https://www.youtube.com"],
        "archive.org": ["https://archive.org", "https://openlibrary.org"],
        "archiveorg": ["https://archive.org", "https://openlibrary.org"],
        "openlibrary": ["https://openlibrary.org"],
        "podcastindex": ["https://podcastindex.org"],
        "loc": ["https://www.loc.gov"],
    }
    return list(defaults.get(label, []))


def ping_first(urls: list[str]) -> tuple[bool, str]:
    for url in urls:
        ok, detail = ping_url(url)
        if ok:
            return True, detail
    if urls:
        return ping_url(urls[0])
    return False, "No ping target"


def collect_plugin_startup_checks(config: dict) -> list[dict[str, Any]]:
    provider_cfg = _provider_config_map(config)

    checks: list[dict[str, Any]] = []
    seen_plugin_keys: set[str] = set()

    for info in _iter_registered_plugin_infos():
        plugin_key = str(getattr(info, "canonical_name", "") or "").strip().lower()
        if not plugin_key:
            continue
        seen_plugin_keys.add(plugin_key)

        plugin = None
        summary: dict[str, Any]
        display_name = provider_display_name(plugin_key)
        configured_entry: Any = None

        try:
            plugin = info.plugin_class(config)
            configured_entry = plugin.plugin_config_root()
            summary = plugin.status_summary()
        except Exception as exc:
            summary = {
                "status": "DISABLED",
                "name": display_name,
                "plugin": plugin_key,
                "detail": str(exc),
            }

        status = str(summary.get("status") or "UNKNOWN").strip().upper() or "UNKNOWN"
        name = str(summary.get("name") or display_name)
        detail = str(summary.get("detail") or "").strip()
        if detail.lower() == "configured" and not configured_entry:
            detail = "Available"
        if not detail:
            if status == "ENABLED":
                detail = "Configured" if configured_entry else "Available"
            else:
                detail = "Not configured" if not configured_entry else "Unavailable"

        checks.append(
            {
                "status": status,
                "name": name,
                "plugin": str(summary.get("plugin") or plugin_key),
                "instance": _resolve_startup_instance_text(
                    plugin,
                    summary,
                    configured_entry if configured_entry else provider_cfg.get(plugin_key),
                ),
                "detail": detail,
                "files": summary.get("files"),
            }
        )

    return checks