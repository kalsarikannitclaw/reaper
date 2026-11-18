#!/bin/sh
# Run any bot command:  ./cnb.sh key set | setup | daily | checkurl URL ...
D="$(cd "$(dirname "$0")" && pwd)"
PYTHONPATH="$D/src" NO_PROXY=localhost,127.0.0.1 exec "$D/.venv/bin/python" -m cnb.cli "$@"
