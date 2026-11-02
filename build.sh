#!/bin/sh
set -eu
librevault_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${PYTHON:-python3}" "$librevault_dir/scripts/build.py" "$@"
