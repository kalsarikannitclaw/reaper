#!/bin/sh
# Linux / macOS-like wrapper: finds Python 3 and runs install.py. Pass --dry-run to preview.
cd "$(dirname "$0")" || exit 1
for p in python3 python; do
  if command -v "$p" >/dev/null 2>&1; then exec "$p" install.py "$@"; fi
done
echo "Python 3 not found. Install Python 3.9+ first (https://www.python.org/downloads/)." >&2; exit 1
