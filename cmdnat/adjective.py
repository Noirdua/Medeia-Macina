import json
import os
import sys
from typing import List, Dict, Any, Sequence, Optional
from SYS.cmdlet_spec import Cmdlet, CmdletArg
from SYS.logger import log
from SYS.result_table import Table
from SYS import pipeline as ctx

ADJECTIVE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "cmdnat",
    "adjective.json"
)
_ADJECTIVE_CACHE: Optional[Dict[str, List[str]]] = None
_ADJECTIVE_CACHE_MTIME_NS: Optional[int] = None


def _load_adjectives() -> Dict[str, List[str]]:
    global _ADJECTIVE_CACHE, _ADJECTIVE_CACHE_MTIME_NS
    try:
        if not os.path.exists(ADJECTIVE_FILE):
            _ADJECTIVE_CACHE = {}
            _ADJECTIVE_CACHE_MTIME_NS = None
            return {}

        current_mtime_ns = os.stat(ADJECTIVE_FILE).st_mtime_ns
        if (_ADJECTIVE_CACHE is not None and
                _ADJECTIVE_CACHE_MTIME_NS == current_mtime_ns):
            return _ADJECTIVE_CACHE

        with open(ADJECTIVE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            loaded = {}

        _ADJECTIVE_CACHE = loaded
        _ADJECTIVE_CACHE_MTIME_NS = current_mtime_ns
        return _ADJECTIVE_CACHE
    except Exception as e:
        log(f"Error loading adjectives: {e}", file=sys.stderr)
    _ADJECTIVE_CACHE = {}
    _ADJECTIVE_CACHE_MTIME_NS = None
    return {}


def _save_adjectives(data: Dict[str, List[str]]) -> bool:
    global _ADJECTIVE_CACHE, _ADJECTIVE_CACHE_MTIME_NS
    try:
        with open(ADJECTIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _ADJECTIVE_CACHE = data
        _ADJECTIVE_CACHE_MTIME_NS = os.stat(ADJECTIVE_FILE).st_mtime_ns
        return True
    except Exception as e:
        log(f"Error saving adjectives: {e}", file=sys.stderr)
        return False


def _run(result: Any, args: Sequence[str], config: Dict[str, Any]) -> int:
    data = _load_adjectives()

    # Parse arguments manually first to handle positional args
    # We expect: .adjective [category] [tag] [-add] [-delete]

    # If no args, list categories
    if not args:
        table = Table("Adjective Categories")
        for i, (category, tags) in enumerate(data.items()):
            row = table.add_row()
            row.add_column("#", str(i + 1))
            row.add_column("Category", category)
            row.add_column("Tag Amount", str(len(tags)))

            # Selection expands to: .adjective "Category Name"
            table.set_row_selection_args(i, [category])

        table.set_source_command(".adjective")
        ctx.set_last_result_table_overlay(table, list(data.keys()))
        ctx.set_current_stage_table(table)
        from SYS.rich_display import stdout_console

        stdout_console().print(table)
        return 0

    # We have args. First arg is likely category.
    category = args[0]

    # Check if we are adding a new category (implicit if it doesn't exist)
    if category not in data:
        # If only category provided, create it
        if len(args) == 1:
            data[category] = []
            _save_adjectives(data)
            log(f"Created new category: {category}")
        # If more args, we might be trying to add to a non-existent category
        elif "-add" in args:
            data[category] = []
            # Continue to add logic

    # Handle operations within category
    remaining_args = list(args[1:])

    # Check for -add flag
    if "-add" in remaining_args:
        # .adjective category -add <value>
        # or .adjective category <value> -add
        add_idx = remaining_args.index("-add")
        # Tag could be before or after
        tag = None
        if add_idx + 1 < len(remaining_args):
            tag = remaining_args[add_idx + 1]
        elif add_idx > 0:
            tag = remaining_args[add_idx - 1]

        if tag:
            if tag not in data[category]:
                data[category].append(tag)
                _save_adjectives(data)
                log(f"Added '{tag}' to '{category}'")
            else:
                log(f"Tag '{tag}' already exists in '{category}'")
        else:
            log("Error: No tag specified to add")
            return 1

    # Check for -delete flag
    elif "-delete" in remaining_args:
        # .adjective category -delete <value>
        # or .adjective category <value> -delete
        del_idx = remaining_args.index("-delete")
        tag = None
        if del_idx + 1 < len(remaining_args):
            tag = remaining_args[del_idx + 1]
        elif del_idx > 0:
            tag = remaining_args[del_idx - 1]

        if tag:
            if tag in data[category]:
                data[category].remove(tag)
                _save_adjectives(data)
                log(f"Deleted '{tag}' from '{category}'")
            else:
                log(f"Tag '{tag}' not found in '{category}'")
        else:
            log("Error: No tag specified to delete")
            return 1

    # List tags in category (Default action if no flags or after modification)
    tags = data.get(category, [])
    table = Table(f"Tags in '{category}'")
    for i, tag in enumerate(tags):
        row = table.add_row()
        row.add_column("#", str(i + 1))
        row.add_column("Tag", tag)

        # Selection expands to: .adjective "Category" "Tag"
        # This allows typing @N -delete to delete it
        table.set_row_selection_args(i, [category, tag])

    table.set_source_command(".adjective")
    ctx.set_last_result_table_overlay(table, tags)
    ctx.set_current_stage_table(table)
    from SYS.rich_display import stdout_console

    stdout_console().print(table)

    return 0


CMDLET = Cmdlet(
    name=".adjective",
    alias=["adj"],
    summary="Manage adjective categories and tags",
    usage=".adjective [category] [-add tag] [-delete tag]",
    arg=[
        CmdletArg(
            name="category",
            type="string",
            description="Category name",
            required=False
        ),
        CmdletArg(name="tag",
                  type="string",
                  description="Tag name",
                  required=False),
        CmdletArg(name="add",
                  type="flag",
                  description="Add tag"),
        CmdletArg(name="delete",
                  type="flag",
                  description="Delete tag"),
    ],
    exec=_run,
)
