#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec "${PYTHON:-python3}" scripts/test_runner.py "$@"
