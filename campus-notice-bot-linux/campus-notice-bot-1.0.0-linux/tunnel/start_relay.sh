#!/bin/sh
# Starts the fetch relay (tunnel) on Linux/macOS/WSL. Ctrl+C stops it.
cd "$(dirname "$0")" || exit 1
exec "$(command -v python3 || command -v python)" relay.py "$@"
