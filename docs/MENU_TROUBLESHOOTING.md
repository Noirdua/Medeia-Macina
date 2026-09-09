# MPV Menu Troubleshooting & Fix Summary

## Problem
Right-clicking in MPV wasn't opening the menu as expected.

## Root Cause
UOSC (the UI overlay script for MPV) claims the `mbtn_right` (right-click) event globally with the `force` flag after loading, which overrides:
- MPV's default input.conf bindings  
- Lua keybindings registered via `mp.add_key_binding()`

UOSC defines its own right-click menu, and there's no straightforward way to override it from a script.

## Current Solution

### Primary Menu Triggers
1. **Press 'm' key** - Opens the Medios Macina menu
2. **Press 'z' key** - Alternative menu trigger
3. **Right-click** - Attempts to trigger via input.conf, but UOSC overrides (needs investigation)

### How It Works
- `plugins/mpv/portable_config/input.conf` routes keybindings to Lua handlers
- `plugins/mpv/LUA/main.lua`:
  - Registers script message handler: `medios-show-menu` 
  - Registers Lua keybindings for 'm' and 'z' keys
  - Both route to `M.show_menu()` which opens an UOSC menu with items

### Menu Structure
The menu calls UOSC's `open-menu` handler with JSON containing:
- Load URL
- Get Metadata  
- Delete File
- Cmd (screenshot, trim, etc)
- Download
- Change Format
- Start Helper (if not running)

## Files Modified
- `plugins/mpv/LUA/main.lua`:
  - Fixed Lua syntax error (extra `end)`)
  - Added comprehensive `[MENU]` and `[KEY]` logging
  - Added 'm' and 'z' keybindings
  - Added `medios-show-menu` script message handler
  - Enhanced `M.show_menu()` with dual methods to call UOSC

- `plugins/mpv/portable_config/input.conf`:
  - Routes `mbtn_right` to `script-message medios-show-menu`
  - Routes 'm' key to `script-message medios-show-menu`

- `plugins/mpv/portable_config/mpv.conf`:
  - Fixed `audio-display` setting (was invalid `yes`, now `no`)

## Testing
Run: `python test_menu.py`

Or manually:
1. Start MPV with: `mpv --script=plugins/mpv/LUA/main.lua --config-dir=plugins/mpv/portable_config --idle`
2. Press 'm' or 'z' key
3. Check logs at `Log/medeia-mpv-lua.log` for `[MENU]` entries

## Next Steps
To properly support right-click:
1. Investigate UOSC's context menu configuration
2. See if UOSC has a hook to add custom menu items to its context menu
3. Or implement a workaround by disabling UOSC's right-click handler

## Logs to Check
- `Log/medeia-mpv-lua.log` - Lua script logs with [MENU], [KEY], [input.conf] tags
- `Log/medeia-mpv-helper.log` - Pipeline helper logs
- Check MPV debug log with: `mpv --log-file=mpv_debug.log --msg-level="all=debug"`
