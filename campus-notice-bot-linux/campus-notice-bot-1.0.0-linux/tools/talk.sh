#!/bin/sh
# Local chat with the Campus Notice Bot (Linux/macOS). Extra arguments go to talk.py, e.g. --terminal
cd "$(dirname "$0")/.." || exit 1
exec python3 tools/talk.py "$@"
