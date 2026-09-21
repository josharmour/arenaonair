#!/usr/bin/env bash
set -e

# Path to the dedicated virtual environment on local disk
VENV_PYTHON="$HOME/.venvs/arenaonair/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "Error: ArenaOnAir virtual environment not found at $VENV_PYTHON" >&2
  echo "Please check ~/.venvs/arenaonair" >&2
  exit 1
fi

exec "$VENV_PYTHON" -m arenaonair.app "$@"
