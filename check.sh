#!/bin/sh
set -eu
cd "$(dirname "$0")"
# Honor $PYTHON. Otherwise take the first interpreter with the required packages,
# whichever name and layout this platform uses.
if [ -z "${PYTHON:-}" ]; then
    for candidate in .venv/bin/python .venv/Scripts/python.exe python3 python; do
        if "$candidate" -c 'import markdown_it, yaml' 2>/dev/null; then
            PYTHON=$candidate
            break
        fi
    done
fi
if [ -z "${PYTHON:-}" ]; then
    echo 'check.sh: no Python with markdown-it-py and PyYAML found. Set PYTHON to one.' >&2
    exit 2
fi
exec "$PYTHON" scripts/test_runner.py "$@"
