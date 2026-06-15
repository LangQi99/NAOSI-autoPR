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

BUFFER_LEVELS = (
    ("1x", 1),
    ("4x", 4),
    ("16x", 16),
)


@dataclass
class DaemonState:
    seen_message_ids: set[str]
    pending_buffers: dict[str, deque[dict[str, Any]]]
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
    build_claude_prompt: Callable[[Config, Path, str, list[dict[str, Any]], Path | None, str], str]
    run_claude: Callable[..., str]
    has_changes: Callable[[Path], bool]
    ensure_committed_and_pushed_to_fork: Callable[[Path, str], str]
    create_pr: Callable[[Path, str, str, Config, Path], None]
    download_message_images: Callable[[list[dict[str, Any]], Path], dict[str, str]]
    render_messages_markdown: Callable[[Config, list[dict[str, Any]], dict[str, str] | None], str]
    log: Callable[[str], None]


def run_daemon_mode(cfg: Config, hooks: DaemonHooks) -> None:
    response_file = cfg.response_file.resolve()
    response_file.parent.mkdir(parents=True, exist_ok=True)
    write_response(response_file, "idle")
    start_response_server(response_file, cfg.response_port, hooks.log)
    state_file = cfg.daemon_state_file.resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)

    daemon_cfg = replace(
        cfg,
        count=max(cfg.count, cfg.daemon_trigger_count * max(multiplier for _, multiplier in BUFFER_LEVELS)),
        claude_timeout_seconds=cfg.daemon_claude_timeout_seconds,
    )
    state = load_daemon_state(state_file, hooks.log)

    hooks.log(
        f"守护启动：trigger={cfg.daemon_trigger_count} poll={cfg.poll_interval_seconds}s "
        f"buffer={state_file.name} resp={cfg.response_port}",
        module="chat",
    )

    while True:
        try:
            credential = hooks.login_webui(daemon_cfg.qq_bot_base, daemon_cfg.qq_bot_token_hash)
            latest = hooks.fetch_group_history(daemon_cfg, credential)
            state.latest_messages = latest
            if not state.initialized:
                state.initialized = True
                persist_daemon_state(state_file, state)
                hooks.log(
                    f"首启回测：历史={len(latest)} trigger={cfg.daemon_trigger_count}",
                    module="chat",
                )
            new_messages = collect_new_messages(latest, state)
            if new_messages:
                persist_daemon_state(state_file, state)
                hooks.log(
                    f"新消息：+{len(new_messages)} buffers={format_buffer_sizes(state)}",
                    module="chat",
                )
                replay_daemon_messages(
                    daemon_cfg,
                    new_messages,
                    state,
                    state_file,
                    response_file,
                    hooks,
                )
            elif run_ready_daemon_buffers(daemon_cfg, state, state_file, response_file, hooks):
                pass
            else:
                hooks.log(f"无新消息：buffers={format_buffer_sizes(state)}", module="chat")
                write_response(response_file, "waiting")
        except Exception as exc:  # noqa: BLE001
            write_response(response_file, f"error\n{exc}")
            hooks.log(f"守护进程异常：{exc}", module="chat")
        time.sleep(cfg.poll_interval_seconds)


def replay_daemon_messages(
    cfg: Config,
    messages: list[dict[str, Any]],
    state: DaemonState,
    state_file: Path,
    response_file: Path,
    hooks: DaemonHooks,
) -> None:
    for msg in messages:
        for label, _ in BUFFER_LEVELS:
            state.pending_buffers.setdefault(label, deque()).append(msg)
        persist_daemon_state(state_file, state)
        run_ready_daemon_buffers(cfg, state, state_file, response_file, hooks)


def run_ready_daemon_buffers(
    cfg: Config,
    state: DaemonState,
    state_file: Path,
    response_file: Path,
    hooks: DaemonHooks,
) -> bool:
    did_run = False
    for label, multiplier in BUFFER_LEVELS:
        threshold = cfg.daemon_trigger_count * multiplier
        buffer = state.pending_buffers.setdefault(label, deque())
        while len(buffer) >= threshold:
            batch = list(buffer)[:threshold]
            run_dir = run_daemon_batch(
                cfg,
                batch,
                state.latest_messages,
                response_file,
                hooks,
                granularity=label,
            )
            state.latest_run_dir = run_dir
            for _ in range(threshold):
                buffer.popleft()
            persist_daemon_state(state_file, state)
            did_run = True
    return did_run


def run_daemon_batch(
    cfg: Config,
    trigger_messages: list[dict[str, Any]],
    latest_messages: list[dict[str, Any]],
    response_file: Path,
    hooks: DaemonHooks,
    *,
    granularity: str,
) -> Path:
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{granularity}-{time.time_ns()}"
    run_dir = cfg.out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_response(response_file, "running")
    hooks.log(
        f"触发处理：run={run_id} granularity={granularity} messages={len(trigger_messages)}",
        module="chat",
    )

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
    prompt = hooks.build_claude_prompt(
        cfg,
        md_path.resolve(),
        branch,
        trigger_messages,
        previous_chat_path,
        granularity,
    )
    prompt_path = run_dir / "claude-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    try:
        response = hooks.run_claude(cfg, prompt, capture_output=True, response_file=response_file)
        write_response(response_file, response.strip() or "(empty response)")
        hooks.log(
            f"处理完成：Claude返回={truncate_text(response.strip() or '(empty response)')}",
            module="chat",
        )
    except subprocess.TimeoutExpired:
        write_response(response_file, "timeout")
        hooks.log("处理超时：Claude 未在限制时间内返回", module="chat")
        raise

    if hooks.has_changes(cfg.repo_dir):
        title = hooks.ensure_committed_and_pushed_to_fork(cfg.repo_dir, cfg.repo_url)
        if cfg.no_pr:
            hooks.log("no-pr：跳过 PR 创建", module="pr")
        else:
            hooks.log("开始创建上游 PR", module="pr")
            hooks.create_pr(cfg.repo_dir, branch, title, cfg, md_path.resolve())
            hooks.log("PR 创建完成", module="pr")
    else:
        hooks.log("处理完成：无代码改动", module="chat")
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


def load_daemon_state(path: Path, log: Callable[[str], None]) -> DaemonState:
    if not path.exists():
        return DaemonState(seen_message_ids=set(), pending_buffers=empty_pending_buffers(), latest_messages=[])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"状态文件读取失败：{path}，将从空状态启动", module="chat")
        return DaemonState(seen_message_ids=set(), pending_buffers=empty_pending_buffers(), latest_messages=[])

    seen = data.get("seen_message_ids") or []
    pending_buffers = load_pending_buffers(data)
    latest = data.get("latest_messages") or []
    initialized = bool(data.get("initialized"))
    latest_run_dir = data.get("latest_run_dir")
    state = DaemonState(
        seen_message_ids={str(item) for item in seen},
        pending_buffers=pending_buffers,
        latest_messages=[item for item in latest if isinstance(item, dict)],
        initialized=initialized,
        latest_run_dir=Path(latest_run_dir) if latest_run_dir else None,
    )
    log(
        f"已加载状态：seen={len(state.seen_message_ids)} buffers={format_buffer_sizes(state)}",
        module="chat",
    )
    return state


def persist_daemon_state(path: Path, state: DaemonState) -> None:
    payload = {
        "seen_message_ids": sorted(state.seen_message_ids),
        "pending_buffers": {
            label: list(state.pending_buffers.get(label, deque()))
            for label, _ in BUFFER_LEVELS
        },
        "latest_messages": state.latest_messages,
        "initialized": state.initialized,
        "latest_run_dir": str(state.latest_run_dir) if state.latest_run_dir else None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def empty_pending_buffers() -> dict[str, deque[dict[str, Any]]]:
    return {label: deque() for label, _ in BUFFER_LEVELS}


def load_pending_buffers(data: dict[str, Any]) -> dict[str, deque[dict[str, Any]]]:
    buffers = empty_pending_buffers()
    raw_buffers = data.get("pending_buffers")
    if isinstance(raw_buffers, dict):
        for label, _ in BUFFER_LEVELS:
            raw_messages = raw_buffers.get(label) or []
            buffers[label] = deque(item for item in raw_messages if isinstance(item, dict))
        return buffers

    raw_legacy_messages = data.get("pending_messages") or []
    buffers["1x"] = deque(item for item in raw_legacy_messages if isinstance(item, dict))
    return buffers


def format_buffer_sizes(state: DaemonState) -> str:
    return ",".join(
        f"{label}={len(state.pending_buffers.get(label, deque()))}"
        for label, _ in BUFFER_LEVELS
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
    log(f"响应服务：0.0.0.0:{port}", module="chat")


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


def truncate_text(text: str, limit: int = 120) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
