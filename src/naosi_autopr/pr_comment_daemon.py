from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from . import Config


@dataclass(frozen=True)
class PRCommentHooks:
    ensure_repo: Callable[[str, Path], None]
    sync_repo: Callable[[Path], None]
    checkout_branch: Callable[[Path, str], None]
    run_claude: Callable[..., str]
    ensure_committed_and_pushed_to_fork: Callable[[Path, str], str]
    gh_api_text: Callable[[list[str], Path], str]
    log: Callable[[str], None]


def run_pr_comment_daemon_mode(cfg: Config, hooks: PRCommentHooks) -> None:
    response_file = cfg.pr_comment_response_file.resolve()
    state_file = cfg.pr_comment_state_file.resolve()
    response_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    write_text(response_file, "idle")
    state = load_state(state_file, hooks.log)

    hooks.log(
        f"守护启动：repo={cfg.pr_comment_repo} poll={cfg.pr_comment_poll_interval_seconds}s "
        f"state={state_file.name}",
        module="pr-comment",
    )

    while True:
        try:
            viewer_login = get_authenticated_login(cfg.pr_comment_local_repo, hooks)
            prs = list_open_auto_prs(cfg.pr_comment_repo)
            if not state.get("initialized"):
                initialize_seen_comments(cfg.pr_comment_repo, prs, state, hooks.log)
                persist_state(state_file, state)
                write_text(response_file, "waiting")
                time.sleep(cfg.pr_comment_poll_interval_seconds)
                continue
            events = collect_new_comment_events(cfg.pr_comment_repo, prs, state)
            if events:
                hooks.log(f"新评论：{len(events)}", module="pr-comment")
                for event in events:
                    handle_comment_event(cfg, event, response_file, viewer_login, hooks)
                persist_state(state_file, state)
            else:
                hooks.log(f"无新评论：open_pr={len(prs)}", module="pr-comment")
                write_text(response_file, "waiting")
        except Exception as exc:  # noqa: BLE001
            write_text(response_file, f"error\n{exc}")
            hooks.log(f"守护进程异常：{exc}", module="pr-comment")
        time.sleep(cfg.pr_comment_poll_interval_seconds)


def list_open_auto_prs(repo: str) -> list[dict[str, Any]]:
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        '"[AUTO]" in:title',
        "--json",
        "number,title,url,body,headRefName,headRepositoryOwner",
    ]
    import subprocess

    result = subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE)
    return json.loads(result.stdout)


def collect_new_comment_events(
    repo: str,
    prs: list[dict[str, Any]],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen_ids = set(state.setdefault("seen_comment_ids", []))
    for pr in prs:
        pr_number = int(pr["number"])
        issue_comments = gh_api_json(f"repos/{repo}/issues/{pr_number}/comments")
        review_comments = gh_api_json(f"repos/{repo}/pulls/{pr_number}/reviews")
        for comment in issue_comments:
            comment_id = f"issue:{comment['id']}"
            if comment_id in seen_ids or is_ignorable_comment(comment):
                continue
            state["seen_comment_ids"].append(comment_id)
            seen_ids.add(comment_id)
            events.append(
                {
                    "pr": pr,
                    "comment_type": "issue_comment",
                    "comment": comment,
                }
            )
        for review in review_comments:
            review_body = (review.get("body") or "").strip()
            if not review_body:
                continue
            review_id = f"review:{review['id']}"
            if review_id in seen_ids or is_ignorable_comment(review):
                continue
            state["seen_comment_ids"].append(review_id)
            seen_ids.add(review_id)
            events.append(
                {
                    "pr": pr,
                    "comment_type": "review",
                    "comment": review,
                }
            )
    return events


def handle_comment_event(
    cfg: Config,
    event: dict[str, Any],
    response_file: Path,
    viewer_login: str,
    hooks: PRCommentHooks,
) -> None:
    pr = event["pr"]
    comment = event["comment"]
    head_owner = str(pr.get("headRepositoryOwner", {}).get("login") or "")
    head_branch = str(pr.get("headRefName") or "").strip()
    if not head_branch:
        hooks.log(f"跳过 PR#{pr['number']}：缺少 head 分支", module="pr-comment")
        return
    if head_owner and head_owner != viewer_login:
        hooks.log(
            f"跳过 PR#{pr['number']}：head 属于 {head_owner}，当前用户={viewer_login}",
            module="pr-comment",
        )
        return
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.out_dir / f"pr-comment-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    pr_url = str(pr["url"])
    pr_body = str(pr.get("body") or "").strip()
    comment_body = str(comment.get("body") or "").strip()
    desc_path = run_dir / "pr-context.md"
    prompt = build_pr_comment_prompt(cfg, pr_url, pr_body, comment_body)
    desc_path.write_text(prompt, encoding="utf-8")

    repo_url = cfg.pr_comment_target_repo_url
    hooks.ensure_repo(repo_url, cfg.pr_comment_local_repo)
    hooks.sync_repo(cfg.pr_comment_local_repo)
    hooks.checkout_branch(cfg.pr_comment_local_repo, head_branch)

    write_text(response_file, "running")
    hooks.log(
        f"处理评论：PR#{pr['number']} branch={head_branch}",
        module="pr-comment",
    )
    response = hooks.run_claude(
        cfg,
        prompt,
        capture_output=True,
        response_file=response_file,
    )
    write_text(response_file, response.strip() or "(empty response)")
    hooks.ensure_committed_and_pushed_to_fork(cfg.pr_comment_local_repo, repo_url)
    hooks.log(
        f"评论处理完成：PR#{pr['number']} Claude返回={truncate_text(response.strip() or '(empty response)')}",
        module="pr-comment",
    )


def build_pr_comment_prompt(cfg: Config, pr_url: str, pr_body: str, comment_body: str) -> str:
    return (
        f"You are working in the repository https://github.com/{cfg.pr_comment_repo}.git.\n\n"
        f"The PR under discussion is:\n{pr_url}\n\n"
        f"PR description:\n{pr_body or '(empty description)'}\n\n"
        f"New comment to address:\n{comment_body}\n\n"
        "Task:\n"
        "- Read the PR description and the new comment carefully.\n"
        "- Make the smallest repository changes necessary to address the comment.\n"
        "- Work only in the dedicated local review clone, not in the chat-daemon repo.\n"
        "- Create a local git commit if you make repository changes.\n"
        "- If you create a git commit, push the current branch to the user's fork remote `fork`.\n"
        "- Do not create a PR.\n"
    )


def is_ignorable_comment(comment: dict[str, Any]) -> bool:
    user = comment.get("user") or {}
    login = str(user.get("login") or "")
    if login.endswith("[bot]"):
        return True
    return False


def load_state(path: Path, log: Callable[[str], None]) -> dict[str, Any]:
    if not path.exists():
        return {"initialized": False, "seen_comment_ids": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("initialized", False)
            data.setdefault("seen_comment_ids", [])
            log(f"已加载评论状态：{path.name}", module="pr-comment")
            return data
    except Exception as exc:  # noqa: BLE001
        log(f"评论状态读取失败：{path} {exc}", module="pr-comment")
    return {"initialized": False, "seen_comment_ids": []}


def persist_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def gh_api_json(path: str) -> list[dict[str, Any]]:
    import subprocess

    result = subprocess.run(
        ["gh", "api", path, "--paginate"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def get_authenticated_login(local_repo: Path, hooks: PRCommentHooks) -> str:
    login = hooks.gh_api_text(["user", "--jq", ".login"], local_repo).strip()
    if not login:
        raise RuntimeError("unable to determine authenticated GitHub login")
    return login


def initialize_seen_comments(
    repo: str,
    prs: list[dict[str, Any]],
    state: dict[str, Any],
    log: Callable[[str], None],
) -> None:
    seen_ids = set(state.setdefault("seen_comment_ids", []))
    for pr in prs:
        pr_number = int(pr["number"])
        issue_comments = gh_api_json(f"repos/{repo}/issues/{pr_number}/comments")
        review_comments = gh_api_json(f"repos/{repo}/pulls/{pr_number}/reviews")
        for comment in issue_comments:
            comment_id = f"issue:{comment['id']}"
            if comment_id not in seen_ids:
                state["seen_comment_ids"].append(comment_id)
                seen_ids.add(comment_id)
        for review in review_comments:
            review_body = (review.get("body") or "").strip()
            if not review_body:
                continue
            review_id = f"review:{review['id']}"
            if review_id not in seen_ids:
                state["seen_comment_ids"].append(review_id)
                seen_ids.add(review_id)
    state["initialized"] = True
    log(
        f"首启基线完成：open_pr={len(prs)} seen_comments={len(state['seen_comment_ids'])}",
        module="pr-comment",
    )


def truncate_text(text: str, limit: int = 120) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
