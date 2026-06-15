#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

chmod +x "$SCRIPT_DIR/_run_common.sh" "$SCRIPT_DIR/run-chat.sh" "$SCRIPT_DIR/run-pr-comment.sh"
source "$SCRIPT_DIR/_run_common.sh"
stop_naosi_processes all

cleanup() {
  local exit_code=$?
  trap - INT TERM EXIT
  if [ -n "${CHAT_PID:-}" ] && kill -0 "$CHAT_PID" >/dev/null 2>&1; then
    kill "$CHAT_PID" >/dev/null 2>&1 || true
  fi
  if [ -n "${PR_COMMENT_PID:-}" ] && kill -0 "$PR_COMMENT_PID" >/dev/null 2>&1; then
    kill "$PR_COMMENT_PID" >/dev/null 2>&1 || true
  fi
  wait >/dev/null 2>&1 || true
  exit "$exit_code"
}

trap cleanup INT TERM EXIT

"$SCRIPT_DIR/run-chat.sh" &
CHAT_PID=$!

"$SCRIPT_DIR/run-pr-comment.sh" &
PR_COMMENT_PID=$!

wait "$CHAT_PID" "$PR_COMMENT_PID"
