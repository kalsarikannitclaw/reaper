#!/bin/sh
# ONE program: refresh, then report + calendar + chat together on http://127.0.0.1:8790/r/calendar.html
# Lives in the package root, next to .venv and reports. Ctrl+C stops it.
cd "$(dirname "$0")" || exit 1
export PYTHONPATH="$PWD/src"
.venv/bin/python -m cnb.cli daily
exec .venv/bin/python -m cnb.cli talk --open
