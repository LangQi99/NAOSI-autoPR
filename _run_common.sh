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

timestamp() {
  date '+%H:%M:%S'
}

shell_log() {
  local module="$1"
  shift
  local now
  now="$(timestamp)"
  if [ -t 1 ]; then
    local reset=$'\033[0m'
    local app_color=$'\033[1;36m'
    local time_color=$'\033[2;37m'
    local module_color=$'\033[1;97m'
    case "$module" in
      launcher) module_color=$'\033[1;95m' ;;
      chat) module_color=$'\033[1;32m' ;;
      pr-comment) module_color=$'\033[1;35m' ;;
      claude) module_color=$'\033[1;33m' ;;
      git) module_color=$'\033[1;34m' ;;
      net) module_color=$'\033[1;36m' ;;
      cmd) module_color=$'\033[0;37m' ;;
      pr) module_color=$'\033[1;31m' ;;
      run|core) module_color=$'\033[1;97m' ;;
    esac
    printf '%s[%s]%s%s[naosi-autopr]%s%s[%s]%s %s%s%s\n' \
      "$time_color" "$now" "$reset" \
      "$app_color" "$reset" \
      "$module_color" "$module" "$reset" \
      "$module_color" "$*" "$reset"
    return
  fi
  printf '[%s][naosi-autopr][%s] %s\n' "$now" "$module" "$*"
}

stop_naosi_processes() {
  local target="${1:-all}"
  local -a patterns=()

  case "$target" in
    chat)
      patterns=('uv run naosi-autopr --daemon')
      ;;
    pr-comment)
      patterns=('uv run naosi-autopr --pr-comment-daemon')
      ;;
    all)
      patterns=(
        'uv run naosi-autopr --daemon'
        'uv run naosi-autopr --pr-comment-daemon'
      )
      ;;
    *)
      echo "error: unsupported stop target: $target" >&2
      return 1
      ;;
  esac

  local pattern pids
  for pattern in "${patterns[@]}"; do
    pids="$(pgrep -f -- "$pattern" || true)"
    if [ -z "$pids" ]; then
      continue
    fi
    shell_log "launcher" "停止旧进程：$pattern pid=$(echo "$pids" | tr '\n' ',' | sed 's/,$//')"
    pkill -TERM -f -- "$pattern" || true
    sleep 1
    if pgrep -f -- "$pattern" >/dev/null 2>&1; then
      shell_log "launcher" "强制停止旧进程：$pattern"
      pkill -KILL -f -- "$pattern" || true
    fi
  done
}
