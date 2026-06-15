from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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


def main() -> None:
    try:
        cfg = parse_args()
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
    parser.add_argument("--claude-budget-usd", default=os.getenv("CLAUDE_BUDGET_USD"))
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
    )


def run(cfg: Config) -> None:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    credential = login_webui(cfg.qq_bot_base, cfg.qq_bot_token_hash)
    print("NapCat WebUI authentication succeeded.")

    ensure_repo(cfg.repo_url, cfg.repo_dir)
    sync_repo(cfg.repo_dir)

    messages = fetch_group_history(cfg, credential)
    if not messages:
        raise AutoPRError("group history returned no messages")

    json_path = run_dir / f"group-{cfg.group_id}.json"
    md_path = run_dir / f"group-{cfg.group_id}.md"
    json_path.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_messages_markdown(cfg, messages), encoding="utf-8")
    print(f"Saved {len(messages)} messages to {md_path}.")

    branch = f"{cfg.branch_prefix}-{run_id}"
    checkout_branch(cfg.repo_dir, branch)

    prompt = build_claude_prompt(cfg, md_path.resolve(), branch, messages)
    prompt_path = run_dir / "claude-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    if cfg.dry_run:
        print(f"Dry run enabled. Claude prompt saved to {prompt_path}.")
        return

    run_claude(cfg, prompt)
    if not has_changes(cfg.repo_dir):
        raise AutoPRError("Claude finished but did not create any repository changes")

    title = build_pr_title(cfg.repo_dir)
    commit_all(cfg.repo_dir, title)
    print(f"Committed local changes with title: {title}")

    if cfg.no_pr:
        print("Skipping push and PR creation because --no-pr was set.")
        return

    create_pr(cfg.repo_dir, branch, title, cfg, md_path.resolve())


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
        cfg = Config(
            qq_bot_base=cfg.qq_bot_base,
            qq_bot_token_hash=cfg.qq_bot_token_hash,
            onebot_base=discovered,
            onebot_token=str(ob11_cfg.get("token") or ""),
            group_id=cfg.group_id,
            count=cfg.count,
            repo_url=cfg.repo_url,
            repo_dir=cfg.repo_dir,
            out_dir=cfg.out_dir,
            branch_prefix=cfg.branch_prefix,
            dry_run=cfg.dry_run,
            no_pr=cfg.no_pr,
            claude_budget_usd=cfg.claude_budget_usd,
        )

    bases = candidate_onebot_bases(cfg)
    errors: list[str] = []
    for base in bases:
        try:
            messages = fetch_group_history_from_base(base, cfg, cfg.onebot_token)
            if messages:
                print(f"Fetched group history from {base}.")
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


def render_messages_markdown(cfg: Config, messages: list[dict[str, Any]]) -> str:
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
        raw = msg.get("raw_message") or msg.get("message") or ""
        if isinstance(raw, list):
            raw = json.dumps(raw, ensure_ascii=False)
        timestamp = format_message_time(msg.get("time"))
        lines.append(f"### {timestamp} - {name}")
        lines.append("")
        lines.append(str(raw).strip() or "(empty message)")
        lines.append("")
    return "\n".join(lines)


def format_message_time(value: Any) -> str:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value).isoformat(timespec="seconds")
    return "unknown-time"


def build_claude_prompt(
    cfg: Config, chat_path: Path, branch: str, messages: list[dict[str, Any]]
) -> str:
    hint = build_repo_hint(messages)
    return textwrap.dedent(
        f"""
        You are working in the repository {cfg.repo_url}.

        Use the QQ group chat export at:
        {chat_path}

        Task:
        - Read the chat export and identify concrete documentation updates requested or implied by the group discussion.
        - Edit this repository only where the chat evidence supports a change.
        - Keep changes focused and reviewable.
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
        {hint}
        """
    ).strip()


def build_repo_hint(messages: list[dict[str, Any]]) -> str:
    joined = "\n".join(str(msg.get("raw_message") or "") for msg in messages)
    if all(token in joined for token in ("培养计划", "开课时间")) and any(
        token in joined for token in ("冲突", "创新", "程序")
    ):
        return (
            "This chat likely belongs in `src/content/docs/course/curricula-variable.mdx`. "
            "Add concise guidance for course time conflicts: check the cultivation plan, check "
            "whether each course opens again in later semesters, and prioritize the course with "
            "the narrower offering window."
        )
    return (
        "Choose the closest existing docs page instead of creating a broad new document. "
        "Only add content that is directly supported by the chat export."
    )


def run_claude(cfg: Config, prompt: str) -> None:
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
    run_cmd(cmd, cwd=cfg.repo_dir, timeout=180)


def ensure_repo(repo_url: str, repo_dir: Path) -> None:
    if (repo_dir / ".git").exists():
        return
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "clone", repo_url, str(repo_dir)])


def sync_repo(repo_dir: Path) -> None:
    run_cmd(["git", "fetch", "--all", "--prune"], cwd=repo_dir)
    run_cmd(["git", "checkout", "main"], cwd=repo_dir)
    run_cmd(["git", "pull", "--ff-only"], cwd=repo_dir)


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
