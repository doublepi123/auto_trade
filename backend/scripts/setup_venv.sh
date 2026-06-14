#!/usr/bin/env bash
# Rebuild a clean Python 3.11+ virtualenv for the auto_trade backend.
#
# Why this exists:
#   - requirements.txt uses ~= caps (P47) for reproducible minor-version range.
#   - requirements.lock.txt pins exact versions for fully reproducible builds.
#   - Existing .venv / .venv312 may have broken symlinks (python3.12 -> host
#     path not resolvable in some sandboxed shells).
#
# Usage (host, with network):
#   ./scripts/setup_venv.sh                  # uses requirements.txt (~= range)
#   ./scripts/setup_venv.sh --locked          # uses requirements.lock.txt (exact)
#   ./scripts/setup_venv.sh --reset           # delete existing .venv first
#
# Requires Python 3.11+ on PATH (CLAUDE.md: "必须 Python 3.11+").

set -euo pipefail

cd "$(dirname "$0")/.."

USE_LOCK=0
RESET=0
for arg in "$@"; do
  case "$arg" in
    --locked) USE_LOCK=1 ;;
    --reset) RESET=1 ;;
    *) echo "unknown arg: $arg"; exit 1 ;;
  esac
done

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python3.12"
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi

if [ ! -f "requirements.txt" ]; then
  echo "ERROR: requirements.txt not found in $(pwd)" >&2
  exit 1
fi

if [ "$RESET" = "1" ] && [ -d ".venv" ]; then
  echo "Removing existing .venv..."
  rm -rf .venv
fi

if [ -d ".venv" ]; then
  echo "Reusing existing .venv. Re-run with --reset to recreate."
else
  echo "Creating .venv with $PYTHON_BIN..."
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Upgrading pip..."
python -m pip install --upgrade pip wheel setuptools

if [ "$USE_LOCK" = "1" ]; then
  if [ ! -f "requirements.lock.txt" ]; then
    echo "ERROR: --locked requested but requirements.lock.txt is missing." >&2
    echo "Generate it via: pip-compile requirements.in --generate-hashes" >&2
    exit 1
  fi
  echo "Installing pinned deps from requirements.lock.txt..."
  python -m pip install --require-hashes -r requirements.lock.txt
else
  echo "Installing range-pinned deps from requirements.txt..."
  python -m pip install -r requirements.txt
  python -m pip install -r requirements-dev.txt
fi

echo
echo "Done. Activate with:"
echo "  source backend/.venv/bin/activate"
echo
echo "Smoke test:"
echo "  cd backend && python -c 'import fastapi, sqlalchemy, pydantic; print(fastapi.__version__, sqlalchemy.__version__, pydantic.VERSION)'"
echo "  cd backend && pytest tests/test_database.py -q"
