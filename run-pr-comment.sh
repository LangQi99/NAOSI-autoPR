#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source "$SCRIPT_DIR/_run_common.sh"
stop_naosi_processes pr-comment

exec uv run naosi-autopr --pr-comment-daemon
