#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Keep Python on local disk, including when the checkout is on a network share.
VENV_DIR="${ARENAONAIR_VENV:-$HOME/.venvs/arenaonair}"
VENV_PYTHON="$VENV_DIR/bin/python"
PROBE='import importlib.util, sys; sys.exit(0 if sys.version_info >= (3, 11) and all(importlib.util.find_spec(m) for m in ("websockets", "kokoro", "numpy", "sounddevice", "PySide6", "pip")) else 1)'

if [ ! -x "$VENV_PYTHON" ] || ! "$VENV_PYTHON" -c "$PROBE" >/dev/null 2>&1; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "ArenaOnAir needs uv to install Python, the window, and voices." >&2
    echo "Install uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
    echo "Then rerun this command. Manual Python setup: docs/install.md" >&2
    exit 1
  fi
  if [ ! -x "$VENV_PYTHON" ]; then
    uv venv --python 3.12 "$VENV_DIR"
  fi
  echo "Installing ArenaOnAir with its window and neural voices (first run can take several minutes)..."
  uv pip install --python "$VENV_PYTHON" -e "$REPO_DIR[tts,ui]"
fi

export PYTHONPATH="$REPO_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV_PYTHON" -m arenaonair.app "$@"
