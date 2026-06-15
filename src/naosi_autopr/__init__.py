from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .daemon import DaemonHooks, run_daemon_mode
from .pr_comment_daemon import PRCommentHooks, run_pr_comment_daemon_mode

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
DEFAULT_PR_COMMENT_REPO = "NAOSI-DLUT/dut-manual"
DEFAULT_PR_COMMENT_LOCAL_REPO = Path("repos/dut-manual-cr")
DEFAULT_PR_COMMENT_TARGET_REPO_URL = "https://github.com/NAOSI-DLUT/dut-manual.git"
DEFAULT_PR_COMMENT_POLL_INTERVAL_SECONDS = 600
DEFAULT_PR_COMMENT_RESPONSE_FILE = Path("pr-comment-response.txt")
DEFAULT_PR_COMMENT_STATE_FILE = Path("pr-comment-state.json")
DEFAULT_PR_COMMENT_BRANCH_PREFIX = "auto/pr-comment"


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
    pr_comment_daemon: bool
    pr_comment_repo: str
    pr_comment_local_repo: Path
    pr_comment_target_repo_url: str
    pr_comment_poll_interval_seconds: int
    pr_comment_response_file: Path
    pr_comment_state_file: Path
    pr_comment_branch_prefix: str

def main() -> None:
    try:
        cfg = parse_args()
        if cfg.pr_comment_daemon:
            run_pr_comment_daemon(cfg)
        elif cfg.daemon:
            run_daemon(cfg)
        else:
            run(cfg)
    except KeyboardInterrupt:
        log("收到停止信号，退出", module="run")
        raise SystemExit(130) from None
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
    parser.add_argument(
        "--pr-comment-daemon",
        action="store_true",
        help="Poll open [AUTO] PR comments and let Claude handle new review feedback in a separate repo.",
    )
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
    parser.add_argument(
        "--pr-comment-repo",
        default=os.getenv("PR_COMMENT_REPO", DEFAULT_PR_COMMENT_REPO),
        help="GitHub repo in owner/name form whose open [AUTO] PR comments should be monitored.",
    )
    parser.add_argument(
        "--pr-comment-local-repo",
        type=Path,
        default=Path(os.getenv("PR_COMMENT_LOCAL_REPO", str(DEFAULT_PR_COMMENT_LOCAL_REPO))),
        help="Local checkout used by the PR-comment daemon for isolated Claude work.",
    )
    parser.add_argument(
        "--pr-comment-target-repo-url",
        default=os.getenv("PR_COMMENT_TARGET_REPO_URL", DEFAULT_PR_COMMENT_TARGET_REPO_URL),
        help="Git URL that the PR-comment daemon edits in its isolated local checkout.",
    )
    parser.add_argument(
        "--pr-comment-poll-interval-seconds",
        type=int,
        default=int(
            os.getenv(
                "PR_COMMENT_POLL_INTERVAL_SECONDS",
                str(DEFAULT_PR_COMMENT_POLL_INTERVAL_SECONDS),
            )
        ),
        help="How often the PR-comment daemon checks for new comments. Default: 600 seconds.",
    )
    parser.add_argument(
        "--pr-comment-response-file",
        type=Path,
        default=Path(
            os.getenv("PR_COMMENT_RESPONSE_FILE", str(DEFAULT_PR_COMMENT_RESPONSE_FILE))
        ),
        help="Overwrite this file with the latest Claude output from the PR-comment daemon.",
    )
    parser.add_argument(
        "--pr-comment-state-file",
        type=Path,
        default=Path(os.getenv("PR_COMMENT_STATE_FILE", str(DEFAULT_PR_COMMENT_STATE_FILE))),
        help="Persist seen PR-comment state to this JSON file.",
    )
    parser.add_argument(
        "--pr-comment-branch-prefix",
        default=os.getenv("PR_COMMENT_BRANCH_PREFIX", DEFAULT_PR_COMMENT_BRANCH_PREFIX),
        help="Branch prefix used by the PR-comment daemon when creating isolated work branches.",
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
        pr_comment_daemon=args.pr_comment_daemon,
        pr_comment_repo=args.pr_comment_repo,
        pr_comment_local_repo=args.pr_comment_local_repo,
        pr_comment_target_repo_url=args.pr_comment_target_repo_url,
        pr_comment_poll_interval_seconds=args.pr_comment_poll_interval_seconds,
        pr_comment_response_file=args.pr_comment_response_file,
        pr_comment_state_file=args.pr_comment_state_file,
        pr_comment_branch_prefix=args.pr_comment_branch_prefix,
    )


def run(cfg: Config) -> None:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"开始执行：run={run_id}", module="run")
    log(f"输出目录：{run_dir}", module="run")

    log("认证 NapCat WebUI", module="net")
    credential = login_webui(cfg.qq_bot_base, cfg.qq_bot_token_hash)
    log("NapCat WebUI 认证成功", module="net")

    log(f"准备仓库：{cfg.repo_dir}", module="git")
    ensure_repo(cfg.repo_url, cfg.repo_dir)
    sync_repo(cfg.repo_dir)

    log(f"拉取群历史：group={cfg.group_id}", module="chat")
    messages = fetch_group_history(cfg, credential)
    if not messages:
        raise AutoPRError("group history returned no messages")
    log(f"拉取完成：messages={len(messages)}", module="chat")

    image_dir = run_dir / "images"
    image_map = download_message_images(messages, image_dir)
    if image_map:
        log(f"图片下载完成：count={len(image_map)}", module="chat")
    else:
        log("无可下载图片", module="chat")

    json_path = run_dir / f"group-{cfg.group_id}.json"
    md_path = run_dir / f"group-{cfg.group_id}.md"
    json_path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_messages_markdown(cfg, messages, image_map), encoding="utf-8")
    log(f"已写入导出：json={json_path.name} md={md_path.name}", module="chat")

    branch = f"{cfg.branch_prefix}-{run_id}"
    log(f"切换分支：{branch}", module="git")
    checkout_branch(cfg.repo_dir, branch)

    prompt = build_claude_prompt(cfg, md_path.resolve(), branch, messages)
    prompt_path = run_dir / "claude-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    log(f"已写入 prompt：{prompt_path.name}", module="claude")

    if cfg.dry_run:
        log("dry-run：停止在 Claude 执行前", module="run")
        return

    log("开始执行 Claude", module="claude")
    run_claude(cfg, prompt)
    log("Claude 执行结束", module="claude")
    title = ensure_committed_and_pushed_to_fork(cfg.repo_dir, cfg.repo_url)

    if cfg.no_pr:
        log("no-pr：跳过 PR 创建", module="pr")
        return

    log("开始创建上游 PR", module="pr")
    create_pr(cfg.repo_dir, branch, title, cfg, md_path.resolve())
    log("PR 创建完成", module="pr")


def run_daemon(cfg: Config) -> None:
    hooks = DaemonHooks(
        login_webui=login_webui,
        fetch_group_history=fetch_group_history,
        ensure_repo=ensure_repo,
        sync_repo=sync_repo,
        checkout_branch=checkout_branch,
        build_claude_prompt=build_claude_prompt,
        run_claude=run_claude,
        has_changes=has_changes,
        ensure_committed_and_pushed_to_fork=ensure_committed_and_pushed_to_fork,
        download_message_images=download_message_images,
        render_messages_markdown=render_messages_markdown,
        log=log,
    )
    run_daemon_mode(cfg, hooks)


def run_pr_comment_daemon(cfg: Config) -> None:
    hooks = PRCommentHooks(
        ensure_repo=ensure_repo,
        sync_repo=sync_repo,
        checkout_branch=checkout_branch,
        run_claude=run_claude,
        ensure_committed_and_pushed_to_fork=ensure_committed_and_pushed_to_fork,
        gh_api_text=gh_api_text,
        log=log,
    )
    run_pr_comment_daemon_mode(cfg, hooks)


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
        cfg = replace(
            cfg,
            onebot_base=discovered,
            onebot_token=str(ob11_cfg.get("token") or ""),
        )

    bases = candidate_onebot_bases(cfg)
    errors: list[str] = []
    for base in bases:
        try:
            messages = fetch_group_history_from_base(base, cfg, cfg.onebot_token)
            if messages:
                log(f"历史拉取成功：source={base} count={len(messages)}", module="net")
                return messages
        except Exception as exc:  # noqa: BLE001
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
        return enabled

    if cfg.onebot_base:
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
    log(
        f"已启用 OneBot HTTP：name={candidate['name']} port={candidate['port']}",
        module="net",
    )
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
                log(f"历史拉取成功：proxy={base} count={len(messages)}", module="net")
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
            log(f"下载图片：{file_name}", module="chat")
            if download_binary(image_url, target):
                image_map[file_name] = str(target)
                log(f"图片已保存：{target.name}", module="chat")
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
        log(f"图片下载失败：url={url} err={exc}", module="chat")
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
        - If you make repository changes, create a local git commit for them.
        - If you create a git commit, push the current branch to the user's fork remote `fork`.
        - Run the relevant verification command if the repository provides one.
        - Do not create a PR; the outer automation will handle PR creation.
        - Treat https://github.com/NAOSI-DLUT/dut-manual as the upstream repository for review context and main-branch syncing.
        - Treat https://github.com/LangQi99/dut-manual as the user's fork that should receive pushes.

        Constraints:
        - PR title must be concise and start with [AUTO].
        - PR body must mention the automation project: {DEFAULT_PROJECT_URL}
        - Current automation branch name: {branch}
        - If you commit, keep using the current automation branch `{branch}`.
        - Prefer the smallest correct edit that captures the concrete guidance from the chat.
        - Do not duplicate information that is already documented in the repository.
        - If existing content is inaccurate, prefer correcting it in place instead of writing a parallel rewrite.
        - If the chat export does not contain enough actionable information, create a short markdown note under docs or the closest existing documentation area explaining that no actionable update was found, instead of inventing content.

        Only add content that is directly supported by the chat export.
        As long as it matches the project, you can add it despite it is not complete and structured.
        because it can be added for the future.
        """
    ).strip()


def run_claude(
    cfg: Config,
    prompt: str,
    *,
    capture_output: bool = False,
    response_file: Path | None = None,
    repo_dir: Path | None = None,
) -> str:
    effective_repo_dir = (repo_dir or cfg.repo_dir).resolve()
    cmd = [
        "claude",
        "-p",
        prompt,
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        str(effective_repo_dir),
    ]
    if cfg.claude_budget_usd:
        cmd.extend(["--max-budget-usd", cfg.claude_budget_usd])
    log(f"执行 claude -p {single_line(prompt, 1000000)}", module="claude")
    log(f"Claude 工作目录：{effective_repo_dir}", module="claude")
    log(f"Claude 超时：{cfg.claude_timeout_seconds}s", module="claude")
    if capture_output:
        return run_cmd_capture(
            cmd,
            cwd=effective_repo_dir,
            timeout=cfg.claude_timeout_seconds,
            response_file=response_file,
        )
    run_cmd(cmd, cwd=effective_repo_dir, timeout=cfg.claude_timeout_seconds)
    return ""


def ensure_repo(repo_url: str, repo_dir: Path) -> None:
    if (repo_dir / ".git").exists():
        return
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "clone", repo_url, str(repo_dir)])


def sync_repo(repo_dir: Path) -> None:
    log("同步上游 main", module="git")
    run_cmd(["git", "fetch", "--all", "--prune"], cwd=repo_dir)
    run_cmd(["git", "checkout", "main"], cwd=repo_dir)
    run_cmd(["git", "reset", "--hard", "origin/main"], cwd=repo_dir)
    log("已对齐 origin/main", module="git")


def checkout_branch(repo_dir: Path, branch: str) -> None:
    try:
        run_cmd(["git", "checkout", "-B", branch], cwd=repo_dir)
    except subprocess.CalledProcessError:
        log(f"checkout失败：清理工作区后重试 branch={branch}", module="git")
        run_cmd(["git", "reset", "--hard", "HEAD"], cwd=repo_dir)
        run_cmd(["git", "clean", "-fd"], cwd=repo_dir)
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


def branch_has_local_commit(repo_dir: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-list", "--count", "main..HEAD"],
        cwd=repo_dir,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return int(result.stdout.strip() or "0") > 0


def current_commit_title(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo_dir,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def current_branch(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_dir,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def branch_pushed_to_remote(repo_dir: Path, remote: str, branch: str) -> bool:
    result = subprocess.run(
        ["git", "ls-remote", "--heads", remote, branch],
        cwd=repo_dir,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return bool(result.stdout.strip())


def ensure_committed_and_pushed_to_fork(repo_dir: Path, repo_url: str) -> str:
    if not has_changes(repo_dir) and not branch_has_local_commit(repo_dir):
        raise AutoPRError("Claude finished but did not create any repository changes or commit")

    if has_changes(repo_dir):
        title = build_pr_title(repo_dir)
        commit_all(repo_dir, title)
        log(f"已提交：{title}", module="git")
    else:
        title = current_commit_title(repo_dir)
        log(f"检测到现有提交：{title}", module="git")

    branch = current_branch(repo_dir)
    login = gh_api_text(["user", "--jq", ".login"], repo_dir).strip()
    if not login:
        raise AutoPRError("unable to determine authenticated GitHub login")
    push_remote = ensure_push_remote(repo_dir, repo_url, login)
    if branch_pushed_to_remote(repo_dir, push_remote, branch):
        log(f"已存在远端分支：{push_remote}/{branch}", module="git")
    else:
        log(f"推送分支：{push_remote}/{branch}", module="git")
        run_cmd(["git", "push", "-u", push_remote, branch], cwd=repo_dir)
    return title


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
    if branch_pushed_to_remote(repo_dir, push_remote, branch):
        log(f"已存在远端分支：{push_remote}/{branch}", module="pr")
    else:
        log(f"推送分支：{push_remote}/{branch}", module="pr")
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
    log(f"已创建 PR：{upstream_owner}/{upstream_repo} <- {login}:{branch}", module="pr")


def build_pr_body(cfg: Config, chat_path: Path) -> str:
    return textwrap.dedent(
        f"""
        由 QQ 群 `{cfg.group_id}` 的聊天记录自动生成。

        自动化使用的聊天记录导出文件：`{chat_path}`

        自动化项目来源：{DEFAULT_PROJECT_URL}
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
    log(f"执行命令：{format_cmd(cmd, full_for_claude=True)}", module="cmd")
    subprocess.run(cmd, cwd=cwd, check=True, timeout=timeout)


def run_cmd_capture(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int | None = None,
    response_file: Path | None = None,
) -> str:
    log(f"执行命令：{format_cmd(cmd, full_for_claude=True)}", module="cmd")
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


def write_response(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def log(message: str, module: str = "core") -> None:
    now = datetime.now().strftime("%H:%M:%S")
    if sys.stdout.isatty():
        reset = "\033[0m"
        app_color = "\033[1;36m"
        time_color = "\033[2;37m"
        module_colors = {
            "chat": "\033[1;32m",
            "pr-comment": "\033[1;35m",
            "claude": "\033[1;33m",
            "git": "\033[1;34m",
            "net": "\033[1;36m",
            "cmd": "\033[0;37m",
            "pr": "\033[1;31m",
            "run": "\033[1;97m",
            "launcher": "\033[1;95m",
            "core": "\033[1;97m",
        }
        module_color = module_colors.get(module, "\033[1;97m")
        line = (
            f"{time_color}[{now}]{reset}"
            f"{app_color}[naosi-autopr]{reset}"
            f"{module_color}[{module}]{reset}"
            " "
            f"{module_color}{message}{reset}"
        )
        print(line, flush=True)
        return
    print(f"[{now}][naosi-autopr][{module}] {message}", flush=True)


def single_line(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def format_cmd(cmd: list[str], limit: int = 200, full_for_claude: bool = False) -> str:
    if full_for_claude and len(cmd) >= 2 and cmd[0] == "claude" and cmd[1] == "-p":
        return single_line(" ".join(cmd), 1000000)
    return single_line(" ".join(cmd), limit)


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
