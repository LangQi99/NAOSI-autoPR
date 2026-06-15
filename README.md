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

## Notes

- `QQ_BOT_TOKEN_HASH` is the SHA256 of `token + ".napcat"`.
- If `QQ_BOT_TOKEN_HASH` is not provided, the tool can derive it from `QQ_BOT_TOKEN`.
- The generated PR body always includes the automation project link: `https://github.com/LangQi99/NAOSI-autoPR`.
- The automation syncs the target checkout with `git fetch --all --prune`, `git checkout main`, and `git pull --ff-only` before creating a work branch.
- Do not commit your real `.env`; keep local NapCat host and token material only in environment variables or an ignored local env file.
