#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

chmod +x "$SCRIPT_DIR/_run_common.sh" "$SCRIPT_DIR/run-chat.sh" "$SCRIPT_DIR/run-pr-comment.sh"

"$SCRIPT_DIR/run-chat.sh" &
CHAT_PID=$!

"$SCRIPT_DIR/run-pr-comment.sh" &
PR_COMMENT_PID=$!

wait "$CHAT_PID" "$PR_COMMENT_PID"
