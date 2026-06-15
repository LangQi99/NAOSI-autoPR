from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from . import Config


@dataclass
class DaemonState:
    seen_message_ids: set[str]
    pending_messages: deque[dict[str, Any]]
    latest_messages: list[dict[str, Any]]
    initialized: bool = False
    latest_run_dir: Path | None = None


@dataclass(frozen=True)
class DaemonHooks:
    login_webui: Callable[[str, str], str]
    fetch_group_history: Callable[[Config, str], list[dict[str, Any]]]
    ensure_repo: Callable[[str, Path], None]
    sync_repo: Callable[[Path], None]
    checkout_branch: Callable[[Path, str], None]
    build_claude_prompt: Callable[[Config, Path, str, list[dict[str, Any]], Path | None], str]
    run_claude: Callable[..., str]
    has_changes: Callable[[Path], bool]
    build_pr_title: Callable[[Path], str]
    commit_all: Callable[[Path, str], None]
    download_message_images: Callable[[list[dict[str, Any]], Path], dict[str, str]]
    render_messages_markdown: Callable[[Config, list[dict[str, Any]], dict[str, str] | None], str]
    log: Callable[[str], None]


def run_daemon_mode(cfg: Config, hooks: DaemonHooks) -> None:
    response_file = cfg.response_file.resolve()
    response_file.parent.mkdir(parents=True, exist_ok=True)
    write_response(response_file, "idle")
    start_response_server(response_file, cfg.response_port, hooks.log)

    daemon_cfg = replace(
        cfg,
        count=max(cfg.count, cfg.daemon_trigger_count),
        no_pr=True,
        claude_timeout_seconds=cfg.daemon_claude_timeout_seconds,
    )
    state = DaemonState(seen_message_ids=set(), pending_messages=deque(), latest_messages=[])

    hooks.log(
        "Daemon mode started. "
        f"Trigger count={cfg.daemon_trigger_count}, poll interval={cfg.poll_interval_seconds}s, "
        f"response file={response_file}, response port={cfg.response_port}."
    )

    while True:
        try:
            credential = hooks.login_webui(daemon_cfg.qq_bot_base, daemon_cfg.qq_bot_token_hash)
            latest = hooks.fetch_group_history(daemon_cfg, credential)
            state.latest_messages = latest
            if not state.initialized:
                for msg in latest:
                    state.seen_message_ids.add(message_identity(msg))
                state.initialized = True
                write_response(response_file, "waiting")
                hooks.log(f"Daemon baseline established from {len(latest)} existing messages.")
                time.sleep(cfg.poll_interval_seconds)
                continue
            new_messages = collect_new_messages(latest, state)
            if new_messages:
                state.pending_messages.extend(new_messages)
                hooks.log(
                    f"Daemon observed {len(new_messages)} new messages. "
                    f"Pending trigger buffer={len(state.pending_messages)}."
                )
            if len(state.pending_messages) >= cfg.daemon_trigger_count:
                batch = list(state.pending_messages)[-cfg.daemon_trigger_count :]
                run_dir = run_daemon_batch(
                    daemon_cfg,
                    batch,
                    state.latest_messages,
                    response_file,
                    hooks,
                )
                state.latest_run_dir = run_dir
                state.pending_messages.clear()
            elif not new_messages:
                write_response(response_file, "waiting")
        except Exception as exc:  # noqa: BLE001
            write_response(response_file, f"error\n{exc}")
            hooks.log(f"Daemon loop failed: {exc}")
        time.sleep(cfg.poll_interval_seconds)


def run_daemon_batch(
    cfg: Config,
    trigger_messages: list[dict[str, Any]],
    latest_messages: list[dict[str, Any]],
    response_file: Path,
    hooks: DaemonHooks,
) -> Path:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_response(response_file, "running")
    hooks.log(f"Daemon triggered run {run_id} from {len(trigger_messages)} new messages.")

    image_dir = run_dir / "images"
    image_map = hooks.download_message_images(trigger_messages, image_dir)
    json_path = run_dir / f"group-{cfg.group_id}.json"
    md_path = run_dir / f"group-{cfg.group_id}.md"
    json_path.write_text(json.dumps(trigger_messages, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(hooks.render_messages_markdown(cfg, trigger_messages, image_map), encoding="utf-8")

    branch = f"{cfg.branch_prefix}-{run_id}"
    hooks.ensure_repo(cfg.repo_url, cfg.repo_dir)
    hooks.sync_repo(cfg.repo_dir)
    hooks.checkout_branch(cfg.repo_dir, branch)

    previous_chat_path = find_previous_chat_export(cfg.out_dir, run_dir)
    prompt = hooks.build_claude_prompt(cfg, md_path.resolve(), branch, trigger_messages, previous_chat_path)
    prompt_path = run_dir / "claude-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    try:
        response = hooks.run_claude(cfg, prompt, capture_output=True, response_file=response_file)
        write_response(response_file, response.strip() or "(empty response)")
    except subprocess.TimeoutExpired:
        write_response(response_file, "timeout")
        raise

    if hooks.has_changes(cfg.repo_dir):
        title = hooks.build_pr_title(cfg.repo_dir)
        hooks.commit_all(cfg.repo_dir, title)
        hooks.log(f"Daemon committed local changes with title: {title}")
    else:
        hooks.log("Daemon run finished with no repository changes.")
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


def write_response(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def start_response_server(response_file: Path, port: int, log: Callable[[str], None]) -> None:
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
