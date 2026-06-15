from __future__ import annotations

import argparse
from collections import deque
import hashlib
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_QQ_BOT_BASE = ""
DEFAULT_QQ_BOT_TOKEN_HASH = ""
DEFAULT_GROUP_ID = 365953299
DEFAULT_TARGET_REPO = "https://github.com/NAOSI-DLUT/dut-manual.git"
DEFAULT_PROJECT_URL = "https://github.com/LangQi99/NAOSI-autoPR"
DEFAULT_REPO_DIR = Path("repos/dut-manual")
DEFAULT_OB11_NAME = "naosi-autopr-http"
DEFAULT_OB11_PORT = 3000
DEFAULT_OB11_TOKEN = "naosi-autopr-token"
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 1800
DEFAULT_DAEMON_TRIGGER_COUNT = 80
DEFAULT_DAEMON_CLAUDE_TIMEOUT_SECONDS = 3600
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_RESPONSE_FILE = Path("response.txt")
DEFAULT_RESPONSE_PORT = 6798
DEFAULT_DAEMON_STATE_FILE = Path("daemon-state.json")


class AutoPRError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    qq_bot_base: str
    qq_bot_token_hash: str
    onebot_base: str | None
    onebot_token: str | None
    group_id: int
    count: int
    repo_url: str
    repo_dir: Path
    out_dir: Path
    branch_prefix: str
    dry_run: bool
    no_pr: bool
    claude_budget_usd: str | None
    claude_timeout_seconds: int
    daemon: bool
    daemon_trigger_count: int
    daemon_claude_timeout_seconds: int
    poll_interval_seconds: int
    response_file: Path
    response_port: int
    daemon_state_file: Path


@dataclass
class DaemonState:
    seen_message_ids: set[str]
    pending_messages: deque[dict[str, Any]]
    latest_messages: list[dict[str, Any]]
    initialized: bool = False
    latest_run_dir: Path | None = None


def main() -> None:
    try:
        cfg = parse_args()
        if cfg.daemon:
            run_daemon(cfg)
        else:
            run(cfg)
    except AutoPRError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        description="Fetch NapCat group history, save it, and ask Claude Code to prepare a PR."
    )
    parser.add_argument("--qq-bot-base", default=os.getenv("QQ_BOT_BASE", DEFAULT_QQ_BOT_BASE))
    parser.add_argument(
        "--qq-bot-token-hash",
        default=os.getenv("QQ_BOT_TOKEN_HASH", DEFAULT_QQ_BOT_TOKEN_HASH),
        help="SHA256(token + '.napcat') for the NapCat WebUI.",
    )
    parser.add_argument(
        "--qq-bot-token",
        default=os.getenv("QQ_BOT_TOKEN"),
        help="Plain NapCat WebUI token. Used only when QQ_BOT_TOKEN_HASH is not set.",
    )
    parser.add_argument(
        "--onebot-base",
        default=os.getenv("ONEBOT_BASE"),
        help="Optional OneBot HTTP base URL, for example http://127.0.0.1:5700.",
    )
    parser.add_argument(
        "--onebot-token",
        default=os.getenv("ONEBOT_TOKEN"),
        help="Optional OneBot access token, if the HTTP API requires one.",
    )
    parser.add_argument("--group-id", type=int, default=int(os.getenv("GROUP_ID", DEFAULT_GROUP_ID)))
    parser.add_argument("--count", type=int, default=int(os.getenv("MESSAGE_COUNT", "80")))
    parser.add_argument("--repo-url", default=os.getenv("TARGET_REPO", DEFAULT_TARGET_REPO))
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path(os.getenv("REPO_DIR", str(DEFAULT_REPO_DIR))),
    )
    parser.add_argument("--out-dir", type=Path, default=Path(os.getenv("OUT_DIR", "runs")))
    parser.add_argument("--branch-prefix", default=os.getenv("BRANCH_PREFIX", "auto/qq-chat"))
    parser.add_argument("--dry-run", action="store_true", help="Export chat and print the Claude prompt only.")
    parser.add_argument("--no-pr", action="store_true", help="Commit locally but do not push or create a PR.")
    parser.add_argument("--daemon", action="store_true", help="Poll group history continuously and trigger runs automatically.")
    parser.add_argument("--claude-budget-usd", default=os.getenv("CLAUDE_BUDGET_USD"))
    parser.add_argument(
        "--claude-timeout-seconds",
        type=int,
        default=int(os.getenv("CLAUDE_TIMEOUT_SECONDS", str(DEFAULT_CLAUDE_TIMEOUT_SECONDS))),
        help="Maximum time to wait for `claude -p` before aborting.",
    )
    parser.add_argument(
        "--daemon-trigger-count",
        type=int,
        default=int(os.getenv("DAEMON_TRIGGER_COUNT", str(DEFAULT_DAEMON_TRIGGER_COUNT))),
        help="In daemon mode, start a new run whenever this many new messages have arrived.",
    )
    parser.add_argument(
        "--daemon-claude-timeout-seconds",
        type=int,
        default=int(
            os.getenv(
                "DAEMON_CLAUDE_TIMEOUT_SECONDS",
                str(DEFAULT_DAEMON_CLAUDE_TIMEOUT_SECONDS),
            )
        ),
        help="In daemon mode, maximum time to wait for `claude -p` before aborting.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=int(os.getenv("POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS))),
        help="In daemon mode, how often to poll for new messages.",
    )
    parser.add_argument(
        "--response-file",
        type=Path,
        default=Path(os.getenv("RESPONSE_FILE", str(DEFAULT_RESPONSE_FILE))),
        help="In daemon mode, overwrite this file with Claude output.",
    )
    parser.add_argument(
        "--response-port",
        type=int,
        default=int(os.getenv("RESPONSE_PORT", str(DEFAULT_RESPONSE_PORT))),
        help="In daemon mode, serve the response file on this TCP port.",
    )
    parser.add_argument(
        "--daemon-state-file",
        type=Path,
        default=Path(os.getenv("DAEMON_STATE_FILE", str(DEFAULT_DAEMON_STATE_FILE))),
        help="In daemon mode, persist seen-message progress and pending buffer to this JSON file.",
    )
    args = parser.parse_args()

    token_hash = args.qq_bot_token_hash
    if not args.qq_bot_base:
        raise AutoPRError("QQ_BOT_BASE is required")
    if not token_hash and args.qq_bot_token:
        token_hash = hashlib.sha256(f"{args.qq_bot_token}.napcat".encode()).hexdigest()
    if not token_hash:
        raise AutoPRError("QQ_BOT_TOKEN_HASH or QQ_BOT_TOKEN is required")

    return Config(
        qq_bot_base=args.qq_bot_base.rstrip("/"),
        qq_bot_token_hash=token_hash,
        onebot_base=args.onebot_base.rstrip("/") if args.onebot_base else None,
        onebot_token=args.onebot_token,
        group_id=args.group_id,
        count=args.count,
        repo_url=args.repo_url,
        repo_dir=args.repo_dir,
        out_dir=args.out_dir,
        branch_prefix=args.branch_prefix,
        dry_run=args.dry_run,
        no_pr=args.no_pr,
        claude_budget_usd=args.claude_budget_usd,
        claude_timeout_seconds=args.claude_timeout_seconds,
        daemon=args.daemon,
        daemon_trigger_count=args.daemon_trigger_count,
        daemon_claude_timeout_seconds=args.daemon_claude_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        response_file=args.response_file,
        response_port=args.response_port,
        daemon_state_file=args.daemon_state_file,
    )


def run(cfg: Config) -> None:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"Starting run {run_id}.")
    log(f"Run artifacts will be written to {run_dir}.")

    log("Authenticating with NapCat WebUI.")
    credential = login_webui(cfg.qq_bot_base, cfg.qq_bot_token_hash)
    log("NapCat WebUI authentication succeeded.")

    log(f"Preparing target repository at {cfg.repo_dir}.")
    ensure_repo(cfg.repo_url, cfg.repo_dir)
    sync_repo(cfg.repo_dir)

    log(f"Fetching group history for {cfg.group_id}.")
    messages = fetch_group_history(cfg, credential)
    if not messages:
        raise AutoPRError("group history returned no messages")
    log(f"Fetched {len(messages)} messages.")

    image_dir = run_dir / "images"
    image_map = download_message_images(messages, image_dir)
    if image_map:
        log(f"Downloaded {len(image_map)} images to {image_dir}.")
    else:
        log("No downloadable images were found in the fetched messages.")

    json_path = run_dir / f"group-{cfg.group_id}.json"
    md_path = run_dir / f"group-{cfg.group_id}.md"
    json_path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_messages_markdown(cfg, messages, image_map), encoding="utf-8")
    log(f"Saved raw history to {json_path}.")
    log(f"Saved markdown export to {md_path}.")

    branch = f"{cfg.branch_prefix}-{run_id}"
    log(f"Checking out working branch {branch}.")
    checkout_branch(cfg.repo_dir, branch)

    prompt = build_claude_prompt(cfg, md_path.resolve(), branch, messages)
    prompt_path = run_dir / "claude-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    log(f"Saved Claude prompt to {prompt_path}.")

    if cfg.dry_run:
        log("Dry run enabled; stopping before Claude execution.")
        return

    log("Starting Claude Code.")
    run_claude(cfg, prompt)
    log("Claude Code finished.")
    if not has_changes(cfg.repo_dir):
        raise AutoPRError("Claude finished but did not create any repository changes")

    title = build_pr_title(cfg.repo_dir)
    commit_all(cfg.repo_dir, title)
    log(f"Committed local changes with title: {title}")

    if cfg.no_pr:
        log("Skipping push and PR creation because --no-pr was set.")
        return

    log("Creating upstream PR.")
    create_pr(cfg.repo_dir, branch, title, cfg, md_path.resolve())
    log("PR creation finished.")


def run_daemon(cfg: Config) -> None:
    response_file = cfg.response_file.resolve()
    response_file.parent.mkdir(parents=True, exist_ok=True)
    write_response(response_file, "idle")
    start_response_server(response_file, cfg.response_port)
    state_file = cfg.daemon_state_file.resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)

    daemon_cfg = replace(
        cfg,
        count=max(cfg.count, cfg.daemon_trigger_count),
        no_pr=True,
        claude_timeout_seconds=cfg.daemon_claude_timeout_seconds,
    )
    state = load_daemon_state(state_file)

    log(
        "Daemon mode started. "
        f"Trigger count={cfg.daemon_trigger_count}, poll interval={cfg.poll_interval_seconds}s, "
        f"response file={response_file}, response port={cfg.response_port}, "
        f"state file={state_file}."
    )

    while True:
        try:
            credential = login_webui(daemon_cfg.qq_bot_base, daemon_cfg.qq_bot_token_hash)
            latest = fetch_group_history(daemon_cfg, credential)
            state.latest_messages = latest
            if not state.initialized:
                ordered_latest = sorted(
                    latest,
                    key=lambda item: (item.get("time") or 0, item.get("message_id") or 0),
                )
                for msg in ordered_latest:
                    state.seen_message_ids.add(message_identity(msg))
                initial_buffer = ordered_latest[-cfg.daemon_trigger_count :]
                state.pending_messages = deque(initial_buffer)
                state.initialized = True
                persist_daemon_state(state_file, state)
                write_response(response_file, "waiting")
                log(
                    f"Daemon baseline established from {len(latest)} existing messages. "
                    f"Initial buffer filled with {len(state.pending_messages)} messages."
                )
            new_messages = collect_new_messages(latest, state)
            if new_messages:
                state.pending_messages.extend(new_messages)
                persist_daemon_state(state_file, state)
                log(
                    f"Daemon observed {len(new_messages)} new messages. "
                    f"Pending trigger buffer={len(state.pending_messages)}."
                )
            if len(state.pending_messages) >= cfg.daemon_trigger_count:
                batch = list(state.pending_messages)[-cfg.daemon_trigger_count :]
                run_dir = run_daemon_batch(daemon_cfg, batch, state.latest_messages, response_file)
                state.latest_run_dir = run_dir
                state.pending_messages.clear()
                persist_daemon_state(state_file, state)
            elif not new_messages:
                write_response(response_file, "waiting")
        except Exception as exc:  # noqa: BLE001
            write_response(response_file, f"error\n{exc}")
            log(f"Daemon loop failed: {exc}")
        time.sleep(cfg.poll_interval_seconds)


def run_daemon_batch(
    cfg: Config,
    trigger_messages: list[dict[str, Any]],
    latest_messages: list[dict[str, Any]],
    response_file: Path,
) -> Path:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_response(response_file, "running")
    log(f"Daemon triggered run {run_id} from {len(trigger_messages)} new messages.")

    image_dir = run_dir / "images"
    image_map = download_message_images(trigger_messages, image_dir)
    json_path = run_dir / f"group-{cfg.group_id}.json"
    md_path = run_dir / f"group-{cfg.group_id}.md"
    json_path.write_text(json.dumps(trigger_messages, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_messages_markdown(cfg, trigger_messages, image_map), encoding="utf-8")

    branch = f"{cfg.branch_prefix}-{run_id}"
    ensure_repo(cfg.repo_url, cfg.repo_dir)
    sync_repo(cfg.repo_dir)
    checkout_branch(cfg.repo_dir, branch)

    previous_chat_path = find_previous_chat_export(cfg.out_dir, run_dir)
    prompt = build_claude_prompt(cfg, md_path.resolve(), branch, trigger_messages, previous_chat_path)
    prompt_path = run_dir / "claude-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    try:
        response = run_claude(cfg, prompt, capture_output=True, response_file=response_file)
        write_response(response_file, response.strip() or "(empty response)")
    except subprocess.TimeoutExpired:
        write_response(response_file, "timeout")
        raise

    if has_changes(cfg.repo_dir):
        title = build_pr_title(cfg.repo_dir)
        commit_all(cfg.repo_dir, title)
        log(f"Daemon committed local changes with title: {title}")
    else:
        log("Daemon run finished with no repository changes.")
    return run_dir


def collect_new_messages(latest: list[dict[str, Any]], state: DaemonState) -> list[dict[str, Any]]:
    new_messages: list[dict[str, Any]] = []
    for msg in sorted(latest, key=lambda item: (item.get("time") or 0, item.get("message_id") or 0)):
        message_key = message_identity(msg)
        if message_key in state.seen_message_ids:
            continue
        state.seen_message_ids.add(message_key)
        new_messages.append(msg)
    return new_messages


def message_identity(msg: dict[str, Any]) -> str:
    message_id = msg.get("message_id")
    if message_id not in (None, ""):
        return f"id:{message_id}"
    return json.dumps(
        {
            "time": msg.get("time"),
            "user_id": msg.get("user_id"),
            "raw_message": msg.get("raw_message"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def load_daemon_state(path: Path) -> DaemonState:
    if not path.exists():
        return DaemonState(seen_message_ids=set(), pending_messages=deque(), latest_messages=[])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"Failed to read daemon state file {path}: {exc}. Starting from empty state.")
        return DaemonState(seen_message_ids=set(), pending_messages=deque(), latest_messages=[])

    seen = data.get("seen_message_ids") or []
    pending = data.get("pending_messages") or []
    latest = data.get("latest_messages") or []
    initialized = bool(data.get("initialized"))
    latest_run_dir = data.get("latest_run_dir")
    state = DaemonState(
        seen_message_ids={str(item) for item in seen},
        pending_messages=deque(item for item in pending if isinstance(item, dict)),
        latest_messages=[item for item in latest if isinstance(item, dict)],
        initialized=initialized,
        latest_run_dir=Path(latest_run_dir) if latest_run_dir else None,
    )
    log(
        f"Loaded daemon state from {path}: "
        f"{len(state.seen_message_ids)} seen ids, {len(state.pending_messages)} buffered messages."
    )
    return state


def persist_daemon_state(path: Path, state: DaemonState) -> None:
    payload = {
        "seen_message_ids": sorted(state.seen_message_ids),
        "pending_messages": list(state.pending_messages),
        "latest_messages": state.latest_messages,
        "initialized": state.initialized,
        "latest_run_dir": str(state.latest_run_dir) if state.latest_run_dir else None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_response(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def start_response_server(response_file: Path, port: int) -> None:
    class ResponseHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = response_file.read_text(encoding="utf-8") if response_file.exists() else ""
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), ResponseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"Serving response file on 0.0.0.0:{port}.")


def find_previous_chat_export(out_dir: Path, current_run_dir: Path) -> Path | None:
    candidates = sorted(
        [path for path in out_dir.iterdir() if path.is_dir() and path.name != current_run_dir.name],
        reverse=True,
    )
    for candidate in candidates:
        md_files = sorted(candidate.glob("group-*.md"))
        if md_files:
            return md_files[0].resolve()
    return None


def login_webui(base: str, token_hash: str) -> str:
    payload = {"hash": token_hash}
    data = request_json(f"{base}/api/auth/login", method="POST", json_payload=payload)
    if data.get("code") != 0:
        raise AutoPRError(f"NapCat WebUI login failed: {data.get('message')}")
    credential = data.get("data", {}).get("Credential")
    if not credential:
        raise AutoPRError("NapCat WebUI login response did not contain Credential")
    return str(credential)


def fetch_group_history(cfg: Config, webui_credential: str) -> list[dict[str, Any]]:
    ob11_cfg = ensure_ob11_http_server(cfg, webui_credential)
    if ob11_cfg and not cfg.onebot_base:
        host = urllib.parse.urlparse(cfg.qq_bot_base).hostname or "127.0.0.1"
        discovered = f"http://{host}:{ob11_cfg['port']}"
        log(f"Using auto-configured OneBot HTTP server at {discovered}.")
        cfg = replace(
            cfg,
            onebot_base=discovered,
            onebot_token=str(ob11_cfg.get("token") or ""),
        )

    bases = candidate_onebot_bases(cfg)
    errors: list[str] = []
    for base in bases:
        try:
            log(f"Trying OneBot history API at {base}.")
            messages = fetch_group_history_from_base(base, cfg, cfg.onebot_token)
            if messages:
                log(f"Fetched group history from {base}.")
                return messages
        except Exception as exc:  # noqa: BLE001
            log(f"OneBot attempt failed at {base}: {exc}")
            errors.append(f"{base}: {exc}")

    proxy_messages = fetch_group_history_via_webui_proxy(cfg, webui_credential, errors)
    if proxy_messages:
        return proxy_messages

    detail = "\n".join(errors[-12:]) if errors else "no candidates were tried"
    raise AutoPRError(
        "unable to fetch group history. Set ONEBOT_BASE to an enabled NapCat OneBot HTTP API.\n"
        f"recent attempts:\n{detail}"
    )


def ensure_ob11_http_server(cfg: Config, webui_credential: str) -> dict[str, Any] | None:
    config = request_json(
        f"{cfg.qq_bot_base}/api/OB11Config/GetConfig",
        method="POST",
        headers={"Authorization": f"Bearer {webui_credential}"},
    )
    if config.get("code") != 0:
        raise AutoPRError(f"OB11Config/GetConfig failed: {config.get('message')}")
    data = config.get("data") or {}
    network = data.setdefault("network", {})
    http_servers = network.setdefault("httpServers", [])

    enabled = next((item for item in http_servers if item.get("enable")), None)
    if enabled:
        log(
            "Found enabled OB11 HTTP server "
            f"{enabled.get('name', '(unnamed)')} on port {enabled.get('port')}."
        )
        return enabled

    if cfg.onebot_base:
        log("ONEBOT_BASE was provided explicitly; skipping OB11 HTTP auto-configuration.")
        return None

    candidate = {
        "enable": True,
        "name": DEFAULT_OB11_NAME,
        "host": "0.0.0.0",
        "port": DEFAULT_OB11_PORT,
        "enableCors": True,
        "enableWebsocket": True,
        "messagePostFormat": "array",
        "token": cfg.onebot_token or DEFAULT_OB11_TOKEN,
        "debug": False,
        "reportSelfMessage": False,
    }
    http_servers[:] = [item for item in http_servers if item.get("name") != DEFAULT_OB11_NAME]
    http_servers.append(candidate)

    saved = request_json(
        f"{cfg.qq_bot_base}/api/OB11Config/SetConfig",
        method="POST",
        json_payload={"config": data},
        headers={"Authorization": f"Bearer {webui_credential}"},
    )
    if saved.get("code") != 0:
        raise AutoPRError(f"OB11Config/SetConfig failed: {saved.get('message')}")

    time.sleep(1)
    log(f"Enabled OB11 HTTP server {candidate['name']} on port {candidate['port']}.")
    return candidate


def candidate_onebot_bases(cfg: Config) -> list[str]:
    candidates: list[str] = []
    if cfg.onebot_base:
        candidates.append(cfg.onebot_base)
    for port in (3000, 3001, 5700, 8080, 6700, 6701):
        candidates.append(f"http://127.0.0.1:{port}")
        host = urllib.parse.urlparse(cfg.qq_bot_base).hostname
        if host:
            candidates.append(f"http://{host}:{port}")
    return dedupe(candidates)


def fetch_group_history_via_webui_proxy(
    cfg: Config, webui_credential: str, errors: list[str]
) -> list[dict[str, Any]]:
    for port in (3000, 3001, 5700, 8080, 6700, 6701):
        base = f"http://127.0.0.1:{port}"
        try:
            messages = fetch_group_history_from_base(
                base,
                cfg,
                cfg.onebot_token,
                proxy_base=cfg.qq_bot_base,
                proxy_credential=webui_credential,
            )
            if messages:
                print(f"Fetched group history via WebUI proxy from {base}.")
                return messages
        except Exception as exc:  # noqa: BLE001
            errors.append(f"proxy {base}: {exc}")
    return []


def fetch_group_history_from_base(
    base: str,
    cfg: Config,
    token: str | None,
    *,
    proxy_base: str | None = None,
    proxy_credential: str | None = None,
) -> list[dict[str, Any]]:
    payload_variants = [
        {"group_id": cfg.group_id, "count": cfg.count},
        {"group_id": str(cfg.group_id), "count": cfg.count, "message_seq": "0", "reverseOrder": True},
        {"group_id": cfg.group_id, "message_seq": 0, "count": cfg.count, "reverse_order": True},
    ]
    endpoints = (
        "/get_group_msg_history",
        "/get_group_msg_history_v2",
        "/get_group_history",
    )
    last_error: Exception | None = None
    for endpoint in endpoints:
        for payload in payload_variants:
            url = f"{base}{endpoint}"
            try:
                data = onebot_request(url, payload, token, proxy_base, proxy_credential)
                messages = extract_messages(data)
                if messages:
                    return messages
            except Exception as exc:  # noqa: BLE001
                last_error = exc
    if last_error:
        raise last_error
    return []


def onebot_request(
    url: str,
    payload: dict[str, Any],
    token: str | None,
    proxy_base: str | None,
    proxy_credential: str | None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if proxy_base:
        raise AutoPRError(
            "NapCat WebUI proxy only supports GET passthrough and cannot relay POST-based history APIs"
        )
    return request_json(url, method="POST", json_payload=payload, headers=headers)


def extract_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("status") == "failed" or data.get("retcode") not in (None, 0):
        raise AutoPRError(str(data))
    payload = data.get("data", data)
    if isinstance(payload, dict):
        for key in ("messages", "message", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    return []


def request_json(
    url: str,
    *,
    method: str = "GET",
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    body = None
    merged_headers = {"Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    if json_payload is not None:
        body = json.dumps(json_payload).encode()
        merged_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=merged_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise AutoPRError(f"{url} returned HTTP {exc.code}: {text[:300]}") from exc
    except urllib.error.URLError as exc:
        raise AutoPRError(f"{url} failed: {exc.reason}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutoPRError(f"{url} did not return JSON: {text[:300]}") from exc


def download_message_images(messages: list[dict[str, Any]], image_dir: Path) -> dict[str, str]:
    image_map: dict[str, str] = {}
    for msg in messages:
        for segment in message_segments(msg):
            if segment.get("type") != "image":
                continue
            data = segment.get("data") or {}
            if not isinstance(data, dict):
                continue
            file_name = str(data.get("file") or "").strip()
            image_url = str(data.get("url") or "").strip()
            if not file_name or not image_url or file_name in image_map:
                continue
            safe_name = sanitize_image_name(file_name)
            image_dir.mkdir(parents=True, exist_ok=True)
            target = image_dir / safe_name
            log(f"Downloading image {file_name} -> {target}.")
            if download_binary(image_url, target):
                image_map[file_name] = str(target)
                log(f"Downloaded image {file_name}.")
    return image_map


def sanitize_image_name(file_name: str) -> str:
    name = Path(file_name).name
    return name or hashlib.sha256(file_name.encode()).hexdigest()


def download_binary(url: str, target: Path, timeout: int = 30) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": "naosi-autopr/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            target.write_bytes(resp.read())
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"warning: failed to download image {url}: {exc}", file=sys.stderr)
        return False


def render_messages_markdown(
    cfg: Config, messages: list[dict[str, Any]], image_map: dict[str, str] | None = None
) -> str:
    lines = [
        f"# QQ Group {cfg.group_id} Chat Export",
        "",
        f"- Exported at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Message count: {len(messages)}",
        f"- Target repository: {cfg.repo_url}",
        f"- Automation repository: {DEFAULT_PROJECT_URL}",
        "",
        "## Messages",
        "",
    ]
    for msg in sorted(messages, key=lambda m: (m.get("time") or 0, m.get("message_id") or 0)):
        sender = msg.get("sender") or {}
        name = sender.get("card") or sender.get("nickname") or msg.get("user_id") or "unknown"
        timestamp = format_message_time(msg.get("time"))
        rendered = render_message_content(msg, image_map or {})
        lines.append(f"### {timestamp} - {name}")
        lines.append("")
        lines.append(rendered or "(empty message)")
        lines.append("")
    return "\n".join(lines)


def render_message_content(msg: dict[str, Any], image_map: dict[str, str]) -> str:
    segments = message_segments(msg)
    if segments:
        parts: list[str] = []
        for segment in segments:
            rendered = render_message_segment(segment, image_map)
            if rendered:
                parts.append(rendered)
        rendered_message = "\n".join(parts).strip()
        if rendered_message:
            return rendered_message

    raw = msg.get("raw_message") or msg.get("message") or ""
    if isinstance(raw, list):
        return "\n".join(
            rendered for rendered in (render_message_segment(item, image_map) for item in raw) if rendered
        ).strip()
    return str(raw).strip()


def message_segments(msg: dict[str, Any]) -> list[dict[str, Any]]:
    segments = msg.get("message")
    if isinstance(segments, list):
        return [item for item in segments if isinstance(item, dict)]
    return []


def render_message_segment(segment: dict[str, Any], image_map: dict[str, str]) -> str:
    segment_type = segment.get("type")
    data = segment.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    if segment_type == "text":
        return str(data.get("text") or "").strip()
    if segment_type == "image":
        file_name = str(data.get("file") or "").strip()
        local_path = image_map.get(file_name)
        summary = summarize_cq_image(segment, file_name)
        if local_path:
            return f"[image: {summary}] {local_path}"
        if file_name:
            return f"[image: {summary}] {file_name}"
        return "[image]"

    raw = json.dumps(segment, ensure_ascii=False)
    return raw.strip()


def summarize_cq_image(segment: dict[str, Any], file_name: str) -> str:
    data = segment.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    summary = str(data.get("summary") or "").strip()
    if summary:
        return html.unescape(summary)
    return file_name or "attachment"


def format_message_time(value: Any) -> str:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value).isoformat(timespec="seconds")
    return "unknown-time"


def build_claude_prompt(
    cfg: Config,
    chat_path: Path,
    branch: str,
    messages: list[dict[str, Any]],
    previous_chat_path: Path | None = None,
) -> str:
    previous_chat_instruction = ""
    if previous_chat_path is not None:
        previous_chat_instruction = textwrap.dedent(
            f"""
            - If the current chat export appears to be missing a very important piece of context, you may inspect the previous chat export at:
              {previous_chat_path}
            - Use the previous chat export only to fill in critical missing context, not to broaden the scope of the change.
            """
        ).strip()
    return textwrap.dedent(
        f"""
        You are working in the repository {cfg.repo_url}.

        Use the QQ group chat export at:
        {chat_path}

        Task:
        - Read the chat export and identify concrete documentation updates requested or implied by the group discussion.
        - If the chat export references local image paths, inspect those images when they are relevant to understanding the discussion.
        {previous_chat_instruction}
        - Edit this repository only where the chat evidence supports a change.
        - Keep changes focused and reviewable.
        - It is acceptable to make no repository changes if the chat does not contain anything clearly valuable enough to document.
        - Run the relevant verification command if the repository provides one.
        - Do not push or create a PR; the outer automation will handle git and PR creation.

        Constraints:
        - PR title must be concise and start with [AUTO].
        - PR body must mention the automation project: {DEFAULT_PROJECT_URL}
        - Current automation branch name: {branch}
        - Prefer the smallest correct edit that captures the concrete guidance from the chat.
        - Do not duplicate information that is already documented in the repository.
        - If existing content is inaccurate, prefer correcting it in place instead of writing a parallel rewrite.
        - If the chat export does not contain enough actionable information, create a short markdown note under docs or the closest existing documentation area explaining that no actionable update was found, instead of inventing content.

        Repository-specific hint:
        Choose the closest existing docs page instead of creating a broad new document.
        Only add content that is directly supported by the chat export.
        """
    ).strip()


def run_claude(
    cfg: Config,
    prompt: str,
    *,
    capture_output: bool = False,
    response_file: Path | None = None,
) -> str:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        str(cfg.repo_dir.resolve()),
    ]
    if cfg.claude_budget_usd:
        cmd.extend(["--max-budget-usd", cfg.claude_budget_usd])
    log(f"Invoking Claude with repository access to {cfg.repo_dir.resolve()}.")
    log(f"Claude timeout is set to {cfg.claude_timeout_seconds} seconds.")
    if capture_output:
        return run_cmd_capture(
            cmd,
            cwd=cfg.repo_dir,
            timeout=cfg.claude_timeout_seconds,
            response_file=response_file,
        )
    run_cmd(cmd, cwd=cfg.repo_dir, timeout=cfg.claude_timeout_seconds)
    return ""


def ensure_repo(repo_url: str, repo_dir: Path) -> None:
    if (repo_dir / ".git").exists():
        return
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "clone", repo_url, str(repo_dir)])


def sync_repo(repo_dir: Path) -> None:
    log("Syncing repository with remotes.")
    run_cmd(["git", "fetch", "--all", "--prune"], cwd=repo_dir)
    run_cmd(["git", "checkout", "main"], cwd=repo_dir)
    run_cmd(["git", "pull", "--ff-only"], cwd=repo_dir)
    log("Repository sync complete.")


def checkout_branch(repo_dir: Path, branch: str) -> None:
    run_cmd(["git", "checkout", "-B", branch], cwd=repo_dir)


def has_changes(repo_dir: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return bool(result.stdout.strip())


def build_pr_title(repo_dir: Path) -> str:
    diff_name = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=repo_dir,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()
    if diff_name:
        changed = Path(diff_name[0]).name
        return f"[AUTO] Update {changed} from QQ chat"
    return "[AUTO] Update dut-manual from QQ chat"


def commit_all(repo_dir: Path, title: str) -> None:
    run_cmd(["git", "add", "-A"], cwd=repo_dir)
    if not has_changes(repo_dir):
        raise AutoPRError("there are no changes to commit after staging")
    run_cmd(["git", "commit", "-m", title], cwd=repo_dir)


def create_pr(repo_dir: Path, branch: str, title: str, cfg: Config, chat_path: Path) -> None:
    if not command_exists("gh"):
        raise AutoPRError("GitHub CLI gh is not installed; cannot create PR")
    auth = subprocess.run(
        ["gh", "auth", "status"],
        cwd=repo_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if auth.returncode != 0:
        raise AutoPRError("gh is not logged in; run `gh auth login` before creating the PR")

    login = gh_api_text(["user", "--jq", ".login"], repo_dir).strip()
    if not login:
        raise AutoPRError("unable to determine authenticated GitHub login")

    push_remote = ensure_push_remote(repo_dir, cfg.repo_url, login)
    log(f"Pushing branch {branch} to remote {push_remote}.")
    run_cmd(["git", "push", "-u", push_remote, branch], cwd=repo_dir)

    upstream_owner, upstream_repo = parse_github_repo(cfg.repo_url)
    body = build_pr_body(cfg, chat_path)
    gh_api_json(
        [
            f"repos/{upstream_owner}/{upstream_repo}/pulls",
            "-X",
            "POST",
            "-f",
            f"title={title}",
            "-f",
            "base=main",
            "-f",
            f"head={login}:{branch}",
            "-f",
            f"body={body}",
        ],
        repo_dir,
    )
    log(f"Created PR against {upstream_owner}/{upstream_repo} from {login}:{branch}.")


def build_pr_body(cfg: Config, chat_path: Path) -> str:
    return textwrap.dedent(
        f"""
        Generated from QQ group `{cfg.group_id}` chat history.

        Chat export used by automation: `{chat_path}`

        Source automation project: {DEFAULT_PROJECT_URL}
        """
    ).strip()


def ensure_push_remote(repo_dir: Path, repo_url: str, login: str) -> str:
    remotes = git_lines(["git", "remote"], repo_dir)
    if "fork" in remotes:
        return "fork"

    origin_url = git_text(["git", "remote", "get-url", "origin"], repo_dir).strip()
    _, origin_repo = parse_github_repo(origin_url)
    if f"github.com/{login}/{origin_repo}" in normalize_git_url(origin_url):
        return "origin"

    _, upstream_repo = parse_github_repo(repo_url)
    fork_url = f"https://github.com/{login}/{upstream_repo}.git"
    run_cmd(["git", "remote", "add", "fork", fork_url], cwd=repo_dir)
    return "fork"


def parse_github_repo(url: str) -> tuple[str, str]:
    normalized = normalize_git_url(url)
    if "github.com/" not in normalized:
        raise AutoPRError(f"unsupported GitHub URL: {url}")
    path = normalized.split("github.com/", 1)[1].strip("/")
    owner, repo = path.split("/", 1)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def normalize_git_url(url: str) -> str:
    if url.startswith("git@github.com:"):
        return "https://github.com/" + url.split("git@github.com:", 1)[1]
    return url


def git_text(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout


def git_lines(cmd: list[str], cwd: Path) -> list[str]:
    return [line for line in git_text(cmd, cwd).splitlines() if line]


def gh_api_text(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["gh", "api", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def gh_api_json(args: list[str], cwd: Path) -> dict[str, Any]:
    text = gh_api_text(args, cwd)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AutoPRError(f"gh api did not return JSON: {text[:300]}") from exc


def run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> None:
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True, timeout=timeout)


def run_cmd_capture(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int | None = None,
    response_file: Path | None = None,
) -> str:
    print("+ " + " ".join(cmd))
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:
        raise AutoPRError("failed to capture command output")

    started = time.monotonic()
    chunks: list[str] = []
    while True:
        if timeout is not None and time.monotonic() - started > timeout:
            process.kill()
            process.wait()
            raise subprocess.TimeoutExpired(cmd, timeout)
        line = process.stdout.readline()
        if line:
            chunks.append(line)
            if response_file is not None:
                write_response(response_file, "".join(chunks))
            continue
        if process.poll() is not None:
            break
        time.sleep(0.1)

    remainder = process.stdout.read()
    if remainder:
        chunks.append(remainder)
        if response_file is not None:
            write_response(response_file, "".join(chunks))

    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd, output="".join(chunks))
    return "".join(chunks)


def log(message: str) -> None:
    print(f"[naosi-autopr] {message}", flush=True)


def command_exists(name: str) -> bool:
    return subprocess.run(["which", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
