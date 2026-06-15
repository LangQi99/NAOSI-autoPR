#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
  echo "error: .venv not found in $SCRIPT_DIR" >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "error: .env not found in $SCRIPT_DIR" >&2
  exit 1
fi

source ".venv/bin/activate"
set -a
source ".env"
set +a
