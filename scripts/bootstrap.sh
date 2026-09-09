#!/usr/bin/env bash
set -e

# Thin POSIX wrapper that delegates to the canonical Python installer
# (scripts/bootstrap.py). Platform bootstraps should prefer calling the
# Python script using --no-delegate and -q/--quiet for quiet/non-interactive mode.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd -P)"

# Prefer repo venv python, then system pythons
if [ -x "$REPO/.venv/bin/python" ]; then
  PY="$REPO/.venv/bin/python"
elif [ -x "$REPO/.venv/bin/python3" ]; then
  PY="$REPO/.venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "Error: No Python interpreter found; please install Python 3 or create the project's .venv." >&2
  exit 1
fi

# Translate -q into --quiet for the Python installer
ARGS=()
for a in "$@"; do
  if [ "$a" = "-q" ]; then
    ARGS+=("--quiet")
  else
    ARGS+=("$a")
  fi
done

exec "$PY" "$REPO/scripts/bootstrap.py" --no-delegate "${ARGS[@]}"
