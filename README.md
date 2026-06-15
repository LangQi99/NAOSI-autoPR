# NAOSI-autoPR

This project exports QQ group chat history from NapCat, saves the history to disk, and asks Claude Code to prepare a documentation PR for `NAOSI-DLUT/dut-manual`.

## What it does

`naosi-autopr` performs these steps:

1. Log in to the NapCat WebUI with `QQ_BOT_BASE` and `QQ_BOT_TOKEN_HASH`.
2. Pull group history for QQ group `365953299` through a NapCat OneBot HTTP API.
3. Save the raw JSON and a readable Markdown export under `runs/<timestamp>/`.
4. Sync `repos/dut-manual`, create a working branch, and call `claude -p` non-interactively.
5. Commit Claude's changes locally and optionally push to your fork and create an upstream PR with a title starting with `[AUTO]`.

The Claude step includes a repository-specific hint generator and a timeout so the automation does not hang indefinitely on weak chat context.

## Daemon mode

`naosi-autopr --daemon` runs continuously instead of exiting after one batch.

In daemon mode it:

1. Polls the QQ group regularly.
2. Tracks newly observed messages.
3. Triggers a Claude run whenever `80` new messages have accumulated.
4. Uses a 1-hour Claude timeout by default in daemon mode.
5. Overwrites `response.txt` with the latest Claude output.
6. Serves that file over HTTP on port `6798`.
7. Persists daemon progress and the pending message buffer to `daemon-state.json`, so restarts can resume without rebuilding the buffer from zero.

Typical usage:

```bash
set -a
source .env
set +a
uv run naosi-autopr --daemon
```

Then read the latest response from:

- local file: `response.txt`
- HTTP: `http://127.0.0.1:6798/`

## Current environment assumptions

- The NapCat WebUI used for authentication must be reachable from the machine running this tool.
- Claude Code is installed and already authenticated on this machine.
- `gh` is required only for the final push/PR creation step, and the authenticated account must have a fork of the target repository.
- A reachable OneBot HTTP API is still required for message-history fetches.
- If no enabled OneBot HTTP server exists in NapCat, the tool will try to create one through `/api/OB11Config/SetConfig` using port `3000`.

The current public host exposes the WebUI port. The tool now configures a public HTTP server on demand when NapCat has no enabled OB11 HTTP server.
For PR creation, the tool detects or adds a `fork` remote that points at `https://github.com/<your-login>/dut-manual.git`, pushes the branch there, and creates the upstream PR through `gh api`.

## Usage

```bash
uv run naosi-autopr \
  --qq-bot-base http://your-napcat-host:6099 \
  --qq-bot-token-hash <sha256-of-token-plus-.napcat> \
  --onebot-base http://127.0.0.1:5700
```

Useful flags:

- `--dry-run`: fetch and export chat, then save the Claude prompt without editing the target repo.
- `--no-pr`: run Claude and commit locally, but skip `git push` and `gh pr create`.
- `--count 120`: control how many history messages to request.
- `--repo-dir repos/dut-manual`: override the target checkout location.
- `--claude-timeout-seconds 1800`: allow Claude up to 30 minutes before the run aborts.
- `--daemon-trigger-count 80`: in daemon mode, trigger after 80 newly observed messages.
- `--daemon-claude-timeout-seconds 3600`: in daemon mode, allow Claude up to 1 hour.
- `--poll-interval-seconds 30`: in daemon mode, poll for new messages every 30 seconds.
- `--response-file response.txt`: in daemon mode, overwrite this file with Claude output.
- `--response-port 6798`: in daemon mode, serve the response file over HTTP.
- `--daemon-state-file daemon-state.json`: in daemon mode, persist seen-message progress and the buffered messages across restarts.

## Environment variables

- `QQ_BOT_BASE`
- `QQ_BOT_TOKEN_HASH`
- `QQ_BOT_TOKEN`
- `ONEBOT_BASE`
- `ONEBOT_TOKEN`
- `GROUP_ID`
- `MESSAGE_COUNT`
- `TARGET_REPO`
- `REPO_DIR`
- `OUT_DIR`
- `BRANCH_PREFIX`
- `CLAUDE_BUDGET_USD`
- `CLAUDE_TIMEOUT_SECONDS`
- `DAEMON_TRIGGER_COUNT`
- `DAEMON_CLAUDE_TIMEOUT_SECONDS`
- `POLL_INTERVAL_SECONDS`
- `RESPONSE_FILE`
- `RESPONSE_PORT`
- `DAEMON_STATE_FILE`

## Notes

- `QQ_BOT_TOKEN_HASH` is the SHA256 of `token + ".napcat"`.
- If `QQ_BOT_TOKEN_HASH` is not provided, the tool can derive it from `QQ_BOT_TOKEN`.
- The generated PR body always includes the automation project link: `https://github.com/LangQi99/NAOSI-autoPR`.
- The automation syncs the target checkout with `git fetch --all --prune`, `git checkout main`, and `git pull --ff-only` before creating a work branch.
- Do not commit your real `.env`; keep local NapCat host and token material only in environment variables or an ignored local env file.
