#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  sh ./scripts/install.sh
fi
exec .venv/bin/python -m bond_perpdex_client.setup "$@"
