 # Bootstrap Installation Troubleshooting Guide

## Problem on "Other Computer"

When running `bootstrap.py` on another machine, the installation completes successfully with messages about creating `mm.bat`, but the `mm` command is not recognized when executed.

**Root Cause:** Windows terminal sessions cache the PATH environment variable. When registry changes are made, existing terminal sessions don't automatically reload the new PATH. The batch shim is created correctly, but it isn't discoverable until either the current session reloads PATH or a new terminal is opened.

## Solution

### Option 1: Restart Terminal (Easiest)
Simply close and reopen your terminal window or PowerShell session. Windows will automatically load the updated PATH from the registry.

```powershell
# Close the current terminal
# Then open a new PowerShell or CMD window
mm --help
```

### Option 2: Reload PATH in Current Session (Manual)
If you don't want to restart, you can reload the PATH in your current PowerShell session:

```powershell
$env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'User') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'Machine')
mm --help
```

Note: The bootstrap installation already attempts a reload for you, but it only affects new commands started after the update (or terminals that already inherit the updated PATH).

## Diagnostic Command

To verify the installation and troubleshoot issues:

```powershell
python scripts/bootstrap.py --check-install
```

This will check:
- ✓ Batch shim exists (`mm.bat`)
- ✓ Shim content is valid and points at the local venv
- ✓ PATH environment variable is configured
- ✓ Registry PATH was updated
- ✓ Test if `mm` command is accessible

### Example Output

```
Checking 'mm' command installation...

Checking for shim files:
   mm.bat: ✓ (%USERPROFILE%\bin\mm.bat)

Checking PATH environment variable:
   %USERPROFILE%\bin in current session PATH: ✗
   %USERPROFILE%\bin in registry PATH: ✗

Testing 'mm' command...
   ✗ 'mm' command not found in PATH
       Shims exist but command is not accessible via PATH

Attempting to call shim directly...
   ✓ Direct shim call works!
       The shim files are valid and functional.

⚠️ 'mm' is not in PATH, but the shim is working correctly.
Possible causes and fixes:
   1. Terminal needs restart: Close and reopen your terminal/PowerShell
   2. PATH reload: Run: $env:Path = [Environment]::GetEnvironmentVariable('PATH', 'User') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'Machine')
   3. Manual PATH: Add %USERPROFILE%\bin to your system PATH manually
```

If the diagnostic shows:
- **✓ Direct shim call works!** → Your installation is fine, just reload PATH
- **✗ Shim files not found** → Re-run `bootstrap.py` without arguments
- **✗ PATH not in registry** → Run bootstrap with `--debug` to see what happened

## Debug Mode

For detailed diagnostic output during installation:

```powershell
python scripts\bootstrap.py --debug
```

This shows:
- Repo path detection
- Venv creation status
- Shim file creation details
- PATH registration results

## Installation Steps (for "Other Computer")

1. **Run bootstrap:**
   ```powershell
   python scripts\bootstrap.py
   ```

2. **Check installation:**
   ```powershell
   python scripts\bootstrap.py --check-install
   ```

3. **If 'mm' is not found:**
   - Close and reopen your terminal (simplest), OR
   - Reload PATH: `$env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'User') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'Machine')`

4. **Verify it works:**
   ```powershell
   mm --help
   ```

## Shim File Locations

The installation creates a Windows shim in:
- **User bin directory:** `C:\Users\<YourUsername>\bin\`
- **File created:** `mm.bat` – a batch shim that both CMD and PowerShell can execute without changing the system execution policy.

The batch shim contains the absolute path to your project and venv, so it always runs the local CLI regardless of the current directory.

## Why This Happens

On Windows, the PATH environment variable is read from the registry when a terminal session starts. If you change the registry PATH while a terminal is open, that terminal doesn't automatically pick up the change. That's why:

1. **New terminals work:** They read the updated registry PATH
2. **Current terminal doesn't work:** It still has the old PATH
3. **Shim files still work:** They can be called directly by absolute path

This is a Windows OS behavior, not a bug in the installation script.

## Manual PATH Addition (If Registry Update Fails)

If for some reason the registry update fails (requires admin rights), you can add the path manually:

1. Open System Properties:
   - Press `Win + X`, select "System"
   - Click "Advanced system settings"

2. Click "Environment Variables"

3. Under "User variables," click "New"
   - Variable name: `PATH`
   - Variable value: `%USERPROFILE%\bin`

4. Click OK and close all terminals

5. Open a new terminal and test `mm --help`

## Next Steps

Once `mm` works:

```powershell
mm --help                    # Show all available commands
mm pipeline --help          # Get help for specific command
mm config --show           # Show current configuration
```

For more information, see the main [README](../readme.md).
