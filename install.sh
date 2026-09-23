#!/bin/sh
set -eu
cd "$(dirname "$0")"
for python in python3.12 python3.13 python3.14 python3; do
  if command -v "$python" >/dev/null 2>&1 && "$python" -c 'import sys; sys.exit(sys.version_info < (3,12))'; then
    "$python" -m venv .venv
    exec .venv/bin/python -m pip install --disable-pip-version-check -e '.[dev]'
  fi
done
printf '%s\n' 'Python 3.12 or newer is required.' >&2
exit 1
