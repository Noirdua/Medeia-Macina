from __future__ import annotations

from typing import Any, Dict, Sequence
import sys

from SYS import pipeline as ctx
from SYS.item_accessors import set_field
from SYS.payload_builders import extract_title_tag_value
from SYS.result_publication import publish_result_table
from .. import _shared as sh

Cmdlet = sh.Cmdlet
CmdletArg = sh.CmdletArg
SharedArgs = sh.SharedArgs
normalize_hash = sh.normalize_hash
parse_tag_arguments = sh.parse_tag_arguments
render_tag_value_templates = sh.render_tag_value_templates
merge_sequences = sh.merge_sequences
extract_tag_from_result = sh.extract_tag_from_result
should_show_help = sh.should_show_help
get_field = sh.get_field
from SYS.logger import debug, log


_DETAIL_PANEL_LIMIT = 9


def _matches_target(
    item: Any,
    target_hash: str | None,
    target_path: str | None,
    target_store: str | None = None,
) -> bool:
    def norm(val: Any) -> str | None:
        return str(val).lower() if val is not None else None

    target_hash_l = target_hash.lower() if target_hash else None
    target_path_l = target_path.lower() if target_path else None
    target_store_l = target_store.lower() if target_store else None

    if isinstance(item, dict):
        hashes = [norm(item.get("hash"))]
        paths = [norm(item.get("path")), norm(item.get("target"))]
        stores = [norm(item.get("store"))]
    else:
        hashes = [norm(get_field(item, "hash"))]
        paths = [norm(get_field(item, "path")), norm(get_field(item, "target"))]
        stores = [norm(get_field(item, "store"))]

    if target_store_l and target_store_l not in stores:
        return False
    if target_hash_l and target_hash_l in hashes:
        return True
    if target_path_l and target_path_l in paths:
        return True
    return False


def _set_result_tags(result: Any, tags: list[str]) -> None:
    normalized = list(tags or [])
    set_field(result, "tag", normalized)

    def _update_tag_columns(columns: Any) -> Any:
        if not isinstance(columns, (list, tuple)):
            return columns

        updated_columns = []
        changed = False
        tag_value = ", ".join(normalized)
        for column in columns:
            if isinstance(column, tuple) and len(column) == 2:
                label, existing_value = column
                if str(label).strip().lower() in {"tag", "tags"}:
                    updated_columns.append((label, tag_value))
                    changed = True
                else:
                    updated_columns.append((label, existing_value))
            elif isinstance(column, list) and len(column) == 2:
                label, existing_value = column
                if str(label).strip().lower() in {"tag", "tags"}:
                    updated_columns.append([label, tag_value])
                    changed = True
                else:
                    updated_columns.append([label, existing_value])
            elif isinstance(column, dict):
                label = column.get("name") or column.get("label") or column.get("key")
                if str(label).strip().lower() in {"tag", "tags"}:
                    updated_column = dict(column)
                    updated_column["value"] = tag_value
                    updated_columns.append(updated_column)
                    changed = True
                else:
                    updated_columns.append(column)
            else:
                updated_columns.append(column)
        if not changed:
            return columns
        if isinstance(columns, tuple):
            return tuple(updated_columns)
        return updated_columns

    if isinstance(result, dict):
        result["tags_flat"] = list(normalized)
        if "tags" in result:
            result["tags"] = list(normalized)
        result["columns"] = _update_tag_columns(result.get("columns"))
        for container_name in ("extra", "metadata", "full_metadata"):
            container = result.get(container_name)
            if not isinstance(container, dict):
                continue
            container["tag"] = list(normalized)
            container["tags_flat"] = list(normalized)
            if "tags" in container:
                container["tags"] = list(normalized)
        return

    try:
        setattr(result, "tags", list(normalized))
    except Exception:
        pass
    try:
        setattr(result, "tags_flat", list(normalized))
    except Exception:
        pass
    try:
        columns = getattr(result, "columns", None)
        updated_columns = _update_tag_columns(columns)
        if updated_columns is not columns:
            setattr(result, "columns", updated_columns)
    except Exception:
        pass
    for container_name in ("extra", "metadata", "full_metadata"):
        container = getattr(result, container_name, None)
        if not isinstance(container, dict):
            continue
        container["tag"] = list(normalized)
        container["tags_flat"] = list(normalized)
        if "tags" in container:
            container["tags"] = list(normalized)


def _apply_title_to_result(result: Any, title_value: str | None) -> None:
    if not title_value:
        return

    if isinstance(result, dict):
        result["title"] = title_value
        cols = result.get("columns")
        if isinstance(cols, list):
            updated_cols = []
            changed = False
            for col in cols:
                if isinstance(col, tuple) and len(col) == 2:
                    label, existing_value = col
                    if str(label).lower() == "title":
                        updated_cols.append((label, title_value))
                        changed = True
                    else:
                        updated_cols.append((label, existing_value))
                else:
                    updated_cols.append(col)
            if changed:
                result["columns"] = updated_cols
        return

    try:
        setattr(result, "title", title_value)
    except Exception:
        pass
    columns = getattr(result, "columns", None)
    if isinstance(columns, list) and columns:
        try:
            label, *_ = columns[0]
            if str(label).lower() == "title":
                columns[0] = (label, title_value)
        except Exception:
            pass


def _refresh_result_table_tags(
    new_tags: list[str],
    target_hash: str | None,
    target_store: str | None,
    target_path: str | None,
) -> None:
    try:
        last_table = ctx.get_last_result_table()
        items = ctx.get_last_result_items()
        if not last_table or not items:
            return

        updated_items = []
        match_found = False
        title_value = extract_title_tag_value(new_tags)
        for item in items:
            try:
                if _matches_target(item, target_hash, target_path, target_store):
                    _set_result_tags(item, new_tags)
                    if title_value:
                        _apply_title_to_result(item, title_value)
                    match_found = True
            except Exception:
                pass
            updated_items.append(item)

        if not match_found:
            return

        new_table = last_table.copy_with_title(getattr(last_table, "title", ""))
        for item in updated_items:
            new_table.add_result(item)

        publish_result_table(ctx, new_table, updated_items, overlay=True)
        try:
            ctx.patch_cached_result_items(
                file_hash=target_hash,
                instance=target_store,
                path=target_path,
                title=title_value or None,
                tags=new_tags,
            )
        except Exception:
            pass
    except Exception:
        pass


def _expand_namespace_delete_tags(tags: Sequence[str], existing_tags: Sequence[str]) -> list[str]:
    expanded: list[str] = []
    existing_list = [str(tag or "").strip() for tag in existing_tags or [] if str(tag or "").strip()]

    for raw_tag in tags or []:
        text = str(raw_tag or "").strip()
        if not text:
            continue
        namespace, sep, value = text.partition(":")
        if sep and namespace.strip() and not value.strip():
            wanted = namespace.strip().casefold()
            matches = []
            for existing in existing_list:
                existing_ns, existing_sep, existing_value = existing.partition(":")
                if not existing_sep:
                    continue
                if existing_ns.strip().casefold() != wanted:
                    continue
                if not existing_value.strip():
                    continue
                matches.append(existing)
            expanded.extend(matches)
            continue
        expanded.append(text)

    return merge_sequences(expanded, case_sensitive=True)


def _refresh_tag_view_if_current(
    file_hash: str | None,
    store_name: str | None,
    path: str | None,
    config: Dict[str,
                 Any]
) -> None:
    """If the current subject matches the target, refresh tags via get-tag."""
    try:
        from cmdlet import get as get_cmdlet  # type: ignore
    except Exception:
        return

    get_tag = None
    try:
        get_tag = get_cmdlet("metadata")
    except Exception:
        get_tag = None
    if not callable(get_tag):
        return

    try:
        subject = ctx.get_last_result_subject()
        if subject is None:
            return

        def norm(val: Any) -> str:
            return str(val).lower()

        target_hash = norm(file_hash) if file_hash else None
        target_path = norm(path) if path else None

        subj_hashes: list[str] = []
        subj_paths: list[str] = []
        if isinstance(subject, dict):
            subj_hashes = [norm(v) for v in [subject.get("hash")] if v]
            subj_paths = [
                norm(v) for v in [subject.get("path"), subject.get("target")] if v
            ]
        else:
            subj_hashes = [
                norm(get_field(subject,
                               f)) for f in ("hash", ) if get_field(subject, f)
            ]
            subj_paths = [
                norm(get_field(subject,
                               f)) for f in ("path", "target") if get_field(subject, f)
            ]

        is_match = False
        if target_hash and target_hash in subj_hashes:
            is_match = True
        if target_path and target_path in subj_paths:
            is_match = True
        if not is_match:
            return

        refresh_args: list[str] = ["-get"]
        if file_hash:
            refresh_args.extend(["-query", f"hash:{file_hash}"])

        # Build a lean subject so get-tag fetches fresh tags instead of reusing cached payloads.
        def _build_refresh_subject() -> Dict[str, Any]:
            payload: Dict[str, Any] = {}
            payload["hash"] = file_hash
            store_value = store_name or get_field(subject, "store")
            if sh.value_has_content(store_value):
                payload["store"] = store_value

            path_value = path or get_field(subject, "path")
            if not sh.value_has_content(path_value):
                path_value = get_field(subject, "target")
            if sh.value_has_content(path_value):
                payload["path"] = path_value

            for key in ("title", "name", "url", "relations", "service_name"):
                val = get_field(subject, key)
                if sh.value_has_content(val):
                    payload[key] = val

            extra_value = get_field(subject, "extra")
            if isinstance(extra_value, dict):
                cleaned = {
                    k: v for k, v in extra_value.items()
                    if str(k).lower() not in {"tag", "tags"}
                }
                if cleaned:
                    payload["extra"] = cleaned
            elif sh.value_has_content(extra_value):
                payload["extra"] = extra_value

            return payload

        refresh_subject = _build_refresh_subject()
        # Do not pass -instance here as it triggers emit_mode/quiet in get-tag
        with ctx.suspend_live_progress():
            get_tag(refresh_subject, refresh_args, config)
    except Exception:
        pass



_DELETE_TAG_CMDLET = Cmdlet(
    name="tag",
    summary="Remove tags from a file in an instance.",
    usage='metadata -delete -instance <instance> [-query "hash:<sha256>"] <tag>[,<tag>...]',
    arg=[
        SharedArgs.QUERY,
        SharedArgs.INSTANCE,
        CmdletArg(
            "<tag>[,<tag>...]",
            required=True,
            description="One or more tags to remove. Comma- or space-separated.",
        ),
    ],
    detail=[
        "- Requires a Hydrus file (hash present) or explicit -query override.",
        "- Multiple tags can be comma-separated or space-separated.",
        "- Use #(namespace) inside a tag value to remove a derived tag, e.g. metadata -delete \"title:#(track) - #(series)\".",
        "- Angle-bracket transforms match metadata -add syntax, e.g. metadata -delete \"code:e<padding(00,#(episode))>\".",
        "- Current documented transforms include padding, default, replace, regex, and increment.",
        "- Template examples assume lowercase tag text; case transforms are intentionally not part of the documented syntax.",
        "- See docs/tag_template_syntax.md for recipe-style examples and the current shared template syntax.",
    ],
)
CMDLET = _DELETE_TAG_CMDLET


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    # Help
    if should_show_help(args):
        log(f"Cmdlet: {_DELETE_TAG_CMDLET.name}\nSummary: {_DELETE_TAG_CMDLET.summary}\nUsage: {_DELETE_TAG_CMDLET.usage}")
        return 0

    def _looks_like_tag_row(obj: Any) -> bool:
        if obj is None:
            return False
        # TagItem (direct) or PipeObject/dict emitted from get-tag table rows.
        try:
            if (hasattr(obj,
                        "__class__") and obj.__class__.__name__ == "TagItem"
                    and hasattr(obj,
                                "tag_name")):
                return True
        except Exception:
            pass
        try:
            return bool(get_field(obj, "tag_name"))
        except Exception:
            return False

    has_piped_tag = _looks_like_tag_row(result)
    has_piped_tag_list = (
        isinstance(result,
                   list) and bool(result) and _looks_like_tag_row(result[0])
    )

    # Parse -query/-instance overrides and collect remaining args.
    override_query: str | None = None
    override_hash: str | None = None
    override_store: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        low = str(a).lower()
        if low in {"-query",
                   "--query",
                   "query"} and i + 1 < len(args):
            override_query = str(args[i + 1]).strip()
            i += 2
            continue
        if low in {"-instance",
                   "--instance"} and i + 1 < len(args):
            override_store = str(args[i + 1]).strip()
            i += 2
            continue
        rest.append(a)
        i += 1

    # Normalize the incoming target list early so a non-hash -query can be treated
    # as the delete-tag payload when the file target already comes from the pipeline.
    items_to_process = sh.normalize_result_items(result)
    tags_arg = parse_tag_arguments(rest)

    override_hash, query_valid = sh.require_single_hash_query(
        override_query,
        "Invalid -query value (expected hash:<sha256>)",
        log_file=sys.stderr,
    )
    if not query_valid:
        if (not tags_arg and override_query and items_to_process and not has_piped_tag
                and not has_piped_tag_list):
            tags_arg = parse_tag_arguments([override_query])
            override_hash = None
        else:
            return 1

    # Selection syntax (@...) is handled by the pipeline runner, not by this cmdlet.
    # If @ reaches here as a literal argument, it's almost certainly user error.
    if rest and str(rest[0]
                    ).startswith("@") and not (has_piped_tag or has_piped_tag_list):
        log("Selection syntax is only supported via piping. Use: @N | metadata -delete")
        return 1

    # Special case: grouped tag selection created by the pipeline runner.
    # This represents "delete these selected tags" (not "delete tags from this file").
    grouped_table = ""
    try:
        grouped_table = str(get_field(result, "table") or "").strip().lower()
    except Exception:
        grouped_table = ""
    grouped_tags = get_field(result, "tag") if result is not None else None
    if (grouped_table == "tag.selection" and isinstance(grouped_tags,
                                                        list) and grouped_tags
            and not tags_arg):
        file_hash = (
            normalize_hash(override_hash)
            if override_hash else normalize_hash(get_field(result,
                                                           "hash"))
        )
        store_name = override_store or get_field(result, "store")
        path = get_field(result, "path") or get_field(result, "target")
        tags = [str(t) for t in grouped_tags if t]
        return 0 if _process_deletion(tags, file_hash, path, store_name, config, result=result) else 1

    if not tags_arg and not has_piped_tag and not has_piped_tag_list:
        log("Requires at least one tag argument")
        return 1

    # Process each item
    success_count = 0
    stage_ctx = ctx.get_stage_context()
    is_last_stage = (stage_ctx is None) or bool(
        getattr(stage_ctx, "is_last_stage", False)
    )
    display_items: list[Any] = []

    # If we have TagItems and no args, we are deleting the tags themselves
    # If we have Files (or other objects) and args, we are deleting tags FROM those files

    # Check if we are in "delete selected tags" mode (tag rows)
    is_tag_item_mode = bool(items_to_process) and _looks_like_tag_row(
        items_to_process[0]
    )

    if is_tag_item_mode:
        # Collect all tags to delete from the TagItems and batch per file.
        # This keeps delete-tag efficient (one backend call per file).
        groups: Dict[tuple[str,
                           str,
                           str],
                     list[str]] = {}
        for item in items_to_process:
            tag_name = get_field(item, "tag_name")
            if not tag_name:
                continue
            item_hash = (
                normalize_hash(override_hash)
                if override_hash else normalize_hash(get_field(item,
                                                               "hash"))
            )
            item_store = override_store or get_field(item, "store")
            item_path = get_field(item, "path") or get_field(item, "target")
            key = (str(item_hash or ""), str(item_store or ""), str(item_path or ""))
            groups.setdefault(key, []).append(str(tag_name))

        for (h, s, p), tag_list in groups.items():
            if not tag_list:
                continue
            if _process_deletion(tag_list, h or None, p or None, s or None, config):
                success_count += 1
        return 0 if success_count > 0 else 1
    else:
        # "Delete tags from files" mode
        # We need args (tags to delete)
        if not tags_arg:
            log("Requires at least one tag argument when deleting from files")
            return 1

    # Collect (store_name, tags_key) -> {backend, hashes, items} groups for bulk dispatch.
    # Items that need per-item existing-tag resolution (e.g. namespace-wildcard expand)
    # are handled individually; static literal tag sets are batched.
    _backend_cache: Dict[str, Any] = {}

    def _get_backend(store_name_str: str) -> Any | None:
        if store_name_str in _backend_cache:
            return _backend_cache[store_name_str]
        try:
            backend, _reg, _exc = sh.get_preferred_store_backend(
                config, store_name_str, suppress_debug=True
            )
        except TypeError:
            backend, _reg, _exc = sh.get_store_backend(
                config, store_name_str, suppress_debug=True
            )
        if backend is not None:
            _backend_cache[store_name_str] = backend
        return backend

    # Bucket: key = (store_name, sorted_tag_tuple) → list of (hash, item, path)
    bulk_groups: Dict[tuple[str, tuple[str, ...]], list[tuple[str, Any, str | None]]] = {}
    items_needing_individual: list[tuple[Any, str, str | None, str]] = []

    tags_has_namespace_wildcard = any(
        (isinstance(t, str) and ":" in t and not t.split(":", 1)[1].strip())
        for t in tags_arg
    )
    tags_has_template = any(
        (isinstance(t, str) and ("$(" in t or "#(" in t or "`(" in t))
        for t in tags_arg
    )
    needs_individual = tags_has_namespace_wildcard or tags_has_template

    for item in items_to_process:
        item_hash = (
            normalize_hash(override_hash)
            if override_hash else normalize_hash(get_field(item, "hash"))
        )
        item_path = get_field(item, "path") or get_field(item, "target")
        item_store = override_store or get_field(item, "store")

        if _looks_like_tag_row(item):
            if tags_arg:
                tags_to_delete = tags_arg
            else:
                tag_name = get_field(item, "tag_name")
                tags_to_delete = [str(tag_name)] if tag_name else []
        else:
            tags_to_delete = tags_arg or []

        if not tags_to_delete or not item_hash or not item_store:
            continue

        store_str = str(item_store)

        # Namespace wildcards (e.g. "album:") and template tags (e.g. "title:#(track)")
        # need existing tags to expand — handle individually.
        if needs_individual:
            items_needing_individual.append((item, item_hash, item_path, store_str))
            continue

        tag_key = tuple(sorted(str(t).strip().lower() for t in tags_to_delete if str(t).strip()))
        bulk_groups.setdefault((store_str, tag_key), []).append((item_hash, item, item_path))

    # --- Bulk dispatch ---
    for (store_str, tag_key), entries in bulk_groups.items():
        backend = _get_backend(store_str)
        if backend is None:
            log(f"Store '{store_str}' not found", file=sys.stderr)
            continue

        hashes = [h for h, _item, _path in entries]
        tag_list = list(tag_key)
        bulk_fn = getattr(backend, "delete_tags_bulk", None)
        bulk_ok = False
        if callable(bulk_fn):
            try:
                bulk_ok = bool(bulk_fn([(h, tag_list) for h in hashes]))
            except Exception:
                bulk_ok = False

        if not bulk_ok:
            add_bulk = getattr(backend, "add_tags_bulk", None)
            if callable(add_bulk):
                try:
                    bulk_ok = bool(add_bulk([(h, [], tag_list) for h in hashes]))
                except Exception:
                    bulk_ok = False
        if not bulk_ok:
            for h in hashes:
                try:
                    backend.delete_tag(h, tag_list, config=config)
                except Exception:
                    pass

        success_count += 1
        delete_set = {t.lower() for t in tag_key}
        for h, item, path in entries:
            # Update in-memory tag list on each result
            old_tags = [str(t) for t in (get_field(item, "tag") or []) if t]
            new_tags = [t for t in old_tags if t.strip().casefold() not in delete_set]
            _set_result_tags(item, new_tags)
            title_value = extract_title_tag_value(new_tags)
            if title_value:
                _apply_title_to_result(item, title_value)
            _refresh_result_table_tags(new_tags, h, store_str, path)
            if is_last_stage:
                display_items.append(item)
            try:
                ctx.emit(item)
            except Exception:
                pass

    # --- Individual dispatch (namespace wildcards) ---
    for item, item_hash, item_path, store_str in items_needing_individual:
        if _process_deletion(tags_arg, item_hash, item_path, store_str, config, result=item):
            success_count += 1
            if is_last_stage:
                display_items.append(item)
        try:
            ctx.emit(item)
        except Exception:
            pass

    if success_count > 0:
        if is_last_stage and display_items:
            try:
                subject = display_items[0] if len(display_items) == 1 else list(display_items)
                display_type = "item" if len(display_items) <= _DETAIL_PANEL_LIMIT else "custom"
                sh.display_and_persist_items(
                    list(display_items),
                    title="Result",
                    subject=subject,
                    display_type=display_type,
                )
            except Exception:
                pass

            try:
                if stage_ctx is not None:
                    stage_ctx.emits = []
            except Exception:
                pass
        return 0
    return 1


def _process_deletion(
    tags: list[str],
    file_hash: str | None,
    path: str | None,
    store_name: str | None,
    config: Dict[str,
                 Any],
    result: Any = None,
) -> bool:
    """Helper to execute the deletion logic for a single target."""

    if not tags:
        return False

    if not store_name:
        log(
            "Store is required (use -instance or pipe a result with store)",
            file=sys.stderr
        )
        return False

    resolved_hash = sh.resolve_hash_for_cmdlet(file_hash, path, None)

    if not resolved_hash:
        log(
            "Item does not include a usable hash (and hash could not be derived from path)",
            file=sys.stderr,
        )
        return False

    def _resolve_backend() -> tuple[Any | None, Any, Exception | None]:
        try:
            return sh.get_preferred_store_backend(
                config,
                store_name,
                suppress_debug=True,
            )
        except TypeError as exc:
            # Some tests monkeypatch get_store_backend with a reduced signature.
            # Fall back so runtime still prefers plugin instance resolution while
            # preserving compatibility with those injected callables.
            if "store_registry" in str(exc):
                return sh.get_store_backend(
                    config,
                    store_name,
                    suppress_debug=True,
                )
            raise

    def _fetch_existing_tags() -> list[str]:
        try:
            backend, _store_registry, _exc = _resolve_backend()
            if backend is None:
                return []
            existing, _src = backend.get_tag(resolved_hash, config=config)
            return list(existing or [])
        except Exception:
            return []

    existing_tag_list = merge_sequences(
        extract_tag_from_result(result),
        _fetch_existing_tags(),
        case_sensitive=True,
    )

    resolved_tags, unresolved_templates = render_tag_value_templates(
        tags,
        existing_tags=existing_tag_list,
        result=result,
    )
    if unresolved_templates:
        log(
            f"[delete_tag] skipped {len(unresolved_templates)} tag template(s) with unresolved #(namespace) placeholders",
            file=sys.stderr,
        )

    tags = _expand_namespace_delete_tags(list(resolved_tags), existing_tag_list)
    if not tags:
        return False

    # Safety: only block if this deletion would remove the final title tag
    title_tags = [
        t for t in tags if isinstance(t, str) and t.lower().startswith("title:")
    ]
    if title_tags:
        existing_tags = existing_tag_list
        current_titles = [
            t for t in existing_tags
            if isinstance(t, str) and t.lower().startswith("title:")
        ]
        del_title_set = {t.lower()
                         for t in title_tags}
        remaining_titles = [t for t in current_titles if t.lower() not in del_title_set]
        if current_titles and not remaining_titles:
            log(
                'Cannot delete the last title: tag. Add a replacement title first (metadata -add "title:new title").',
                file=sys.stderr,
            )
            return False

    try:
        backend, _store_registry, exc = _resolve_backend()
        if backend is None:
            raise exc or KeyError(store_name)
        ok = backend.delete_tag(resolved_hash, list(tags), config=config)
        if ok:
            refreshed_tags: list[str] = []
            try:
                refreshed, _src = backend.get_tag(resolved_hash, config=config)
                refreshed_tags = list(refreshed or [])
            except Exception:
                delete_set = {str(tag).strip().casefold() for tag in tags}
                refreshed_tags = [
                    existing_tag for existing_tag in existing_tag_list
                    if str(existing_tag).strip().casefold() not in delete_set
                ]

            if result is not None:
                _set_result_tags(result, refreshed_tags)
                title_value = extract_title_tag_value(refreshed_tags)
                if title_value:
                    _apply_title_to_result(result, title_value)

            _refresh_result_table_tags(refreshed_tags, resolved_hash, store_name, path)
            _refresh_tag_view_if_current(resolved_hash, store_name, path, config)
            return True
        return False
    except Exception as exc:
        log(f"del-tag failed: {exc}")
        return False


CMDLET.exec = _run

