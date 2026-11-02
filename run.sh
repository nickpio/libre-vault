#!/bin/sh
set -eu
librevault_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$librevault_dir${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m librevault "$@"
