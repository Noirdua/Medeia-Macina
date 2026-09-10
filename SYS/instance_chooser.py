from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from SYS.logger import log


def _token_value(args: Sequence[str], *flags: str) -> Optional[str]:
    tokens = [str(t or "") for t in (args or [])]
    wanted = {f.lower() for f in flags}
    for idx, tok in enumerate(tokens):
        low = tok.replace("_", "-").strip().lower()
        if low in wanted and idx + 1 < len(tokens):
            nxt = str(tokens[idx + 1] or "").strip()
            if nxt and not nxt.startswith("-"):
                return nxt
        if "=" in low:
            name, _, val = tok.partition("=")
            if name.replace("_", "-").strip().lower() in wanted and val.strip():
                return val.strip()
    return None


def _instance_from_query(args: Sequence[str]) -> Optional[str]:
    query = _token_value(args, "-query", "--query")
    if not query:
        return None
    text = str(query).strip().strip('"').strip("'")
    for part in text.replace(";", ",").split(","):
        chunk = part.strip()
        if ":" not in chunk:
            continue
        key, _, value = chunk.partition(":")
        if key.strip().lower() == "instance" and value.strip():
            return value.strip().strip('"').strip("'")
    return None


def stage_has_instance(args: Sequence[str]) -> bool:
    return bool(_token_value(args, "-instance", "--instance") or _instance_from_query(args))


def list_plugin_instance_names(plugin_name: str, config: Dict[str, Any]) -> List[str]:
    name = str(plugin_name or "").strip()
    if not name:
        return []
    try:
        from PluginCore.registry import get_plugin

        plugin = get_plugin(name, config)
    except Exception:
        plugin = None
    if plugin is None:
        return []
    names: List[str] = []
    try:
        names = [str(n).strip() for n in (plugin.configured_instances() or []) if str(n).strip()]
    except Exception:
        names = []
    if not names:
        extra = getattr(plugin, "_configured_store_names", None)
        if callable(extra):
            try:
                names = [str(n).strip() for n in (extra() or []) if str(n).strip()]
            except Exception:
                names = []
    filtered = [n for n in names if n.lower() != "default"]
    return filtered or names


def _inject_instance(tokens: Sequence[str], instance_name: str) -> List[str]:
    out = [str(t) for t in (tokens or [])]
    if stage_has_instance(out):
        return out
    return out + ["-instance", str(instance_name)]


def _flatten_stages(stages: Sequence[Sequence[str]]) -> List[str]:
    tokens: List[str] = []
    for idx, stage in enumerate(stages or []):
        if idx:
            tokens.append("|")
        tokens.extend(str(t) for t in stage)
    return tokens


def pipeline_target_instance_names(
    stages: Sequence[Sequence[str]] | None = None,
) -> List[str]:
    names: List[str] = []
    if stages:
        for stage in stages:
            inst = _token_value(stage, "-instance", "--instance") or _instance_from_query(stage)
            if inst and inst not in names:
                names.append(inst)
        return names
    try:
        from SYS import pipeline as ctx

        stored = ctx.load_value("preflight.target_instances", default=None)
    except Exception:
        stored = None
    if isinstance(stored, list):
        return [str(n).strip() for n in stored if str(n).strip()]
    return []


def filter_backends_to_pipeline_instances(backend_names: Sequence[str]) -> List[str]:
    wanted = {n.strip().lower() for n in pipeline_target_instance_names() if str(n).strip()}
    raw = [str(n).strip() for n in (backend_names or []) if str(n).strip()]
    if not wanted:
        return raw
    filtered = [n for n in raw if n.lower() in wanted]
    return filtered or raw


def store_pipeline_target_instances(stages: Sequence[Sequence[str]]) -> List[str]:
    names = pipeline_target_instance_names(stages)
    try:
        from SYS import pipeline as ctx

        ctx.store_value("preflight.target_instances", names)
        cache = ctx.load_value("preflight", default=None)
        if not isinstance(cache, dict):
            cache = {}
        cache["target_instances"] = names
        ctx.store_value("preflight", cache)
    except Exception:
        pass
    return names


def first_unresolved_plugin(
    stages: Sequence[Sequence[str]],
    config: Dict[str, Any],
) -> Optional[str]:
    for stage in stages or []:
        plugin_name = _token_value(stage, "-plugin", "--plugin")
        if not plugin_name or stage_has_instance(stage):
            continue
        instances = list_plugin_instance_names(plugin_name, config)
        if len(instances) > 1:
            return plugin_name
    return None


def replay_stages_with_instance(
    stages: Sequence[Sequence[str]],
    plugin_name: str,
    instance_name: str,
) -> List[str]:
    wanted = str(plugin_name or "").strip().lower()
    replay: List[List[str]] = []
    for stage in stages or []:
        stage_plugin = _token_value(stage, "-plugin", "--plugin")
        if (
            stage_plugin
            and str(stage_plugin).strip().lower() == wanted
            and not stage_has_instance(stage)
        ):
            replay.append(_inject_instance(stage, instance_name))
        else:
            replay.append([str(t) for t in stage])
    return _flatten_stages(replay)


def _publish_instance_table(
    plugin_name: str,
    instances: Sequence[str],
    *,
    command: str,
    continue_args: Sequence[str],
    selection_action: Callable[[str], Sequence[str]],
) -> bool:
    from SYS.result_table import Table
    from SYS.result_publication import publish_result_table
    from SYS import pipeline as ctx

    table = Table(f"{plugin_name} instances")
    table.set_table("plugin.instances")
    table.set_source_command(command, [str(t) for t in (continue_args or []) if str(t).strip()])
    rows: List[Dict[str, Any]] = []
    for name in instances:
        action = [str(t) for t in selection_action(name) if t is not None]
        payload = {
            "title": name,
            "plugin": plugin_name,
            "instance": name,
            "columns": [
                ("Instance", name),
                ("Plugin", plugin_name),
            ],
            "_selection_action": action,
            "_selection_args": ["-instance", name],
        }
        table.add_result(payload)
        rows.append(payload)
    setattr(table, "_items_added", True)
    publish_result_table(ctx, table, rows, overlay=False)
    try:
        from SYS.rich_display import stdout_console

        stdout_console().print(table)
        setattr(table, "_rendered_by_cmdlet", True)
    except Exception:
        pass
    log(f"Select an instance with @N ({len(instances)} {plugin_name} instances).")
    return True


def maybe_publish_instance_chooser(
    args: Sequence[str],
    config: Dict[str, Any],
    *,
    command: str = "file",
) -> bool:
    plugin_name = _token_value(args, "-plugin", "--plugin")
    if not plugin_name:
        return False
    if stage_has_instance(args):
        return False
    instances = list_plugin_instance_names(plugin_name, config)
    if len(instances) <= 1:
        return False

    continue_args = [str(t) for t in (args or []) if str(t).strip()]
    return _publish_instance_table(
        plugin_name,
        instances,
        command=command,
        continue_args=continue_args,
        selection_action=lambda name: [command, *continue_args, "-instance", name],
    )


def maybe_publish_pipeline_instance_chooser(
    stages: Sequence[Sequence[str]],
    config: Dict[str, Any],
) -> bool:
    resolved = [list(stage) for stage in (stages or []) if stage]
    if not resolved:
        return False
    plugin_name = first_unresolved_plugin(resolved, config)
    if not plugin_name:
        return False
    instances = list_plugin_instance_names(plugin_name, config)
    if len(instances) <= 1:
        return False

    command = str(resolved[0][0] or "file")
    continue_args = [str(t) for t in resolved[0][1:]]
    return _publish_instance_table(
        plugin_name,
        instances,
        command=command,
        continue_args=continue_args,
        selection_action=lambda name: replay_stages_with_instance(
            resolved, plugin_name, name
        ),
    )
