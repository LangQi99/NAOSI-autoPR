from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from . import Config

MAX_COMMENT_BODY_LENGTH = 4000


@dataclass(frozen=True)
class PRCommentHooks:
    ensure_repo: Callable[[str, Path], None]
    sync_repo: Callable[[Path], None]
    checkout_pr_head: Callable[[Path, str, str, str], None]
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
            hooks.ensure_repo(cfg.pr_comment_target_repo_url, cfg.pr_comment_local_repo)
            viewer_login = get_authenticated_login(cfg.pr_comment_local_repo, hooks)
            prs = list_open_auto_prs(cfg.pr_comment_repo)
            if not state.get("initialized"):
                initialize_seen_comments(cfg.pr_comment_repo, prs, state, hooks.log)
                persist_state(state_file, state)
                write_text(response_file, "waiting")
                time.sleep(cfg.pr_comment_poll_interval_seconds)
                continue
            events = collect_new_comment_events(cfg.pr_comment_repo, prs, state)
            pr_events = group_events_by_pr(events)
            if pr_events:
                hooks.log(
                    f"新评论={len(events)} 涉及PR={len(pr_events)}",
                    module="pr-comment",
                )
                pr_number, selected_events = select_next_pr_events(pr_events)
                should_mark_seen = handle_pr_events(
                    cfg,
                    selected_events,
                    response_file,
                    viewer_login,
                    hooks,
                )
                if should_mark_seen:
                    mark_events_seen(state, selected_events)
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
        issue_comments, review_comments, review_line_comments = fetch_pr_comment_payloads(
            repo,
            pr_number,
        )
        resolved_review_comment_ids = get_resolved_review_comment_ids(repo, pr_number)
        for comment in issue_comments:
            comment_id = f"issue-comment:{comment['id']}"
            if comment_id in seen_ids or is_ignorable_comment(comment):
                continue
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
            events.append(
                {
                    "pr": pr,
                    "comment_type": "review",
                    "comment": review,
                }
            )
        for comment in review_line_comments:
            comment_id = f"review-comment:{comment['id']}"
            if comment_id in seen_ids or is_ignorable_comment(comment):
                continue
            if int(comment["id"]) in resolved_review_comment_ids:
                continue
            events.append(
                {
                    "pr": pr,
                    "comment_type": "review_comment",
                    "comment": comment,
                }
            )
    return events


def handle_pr_events(
    cfg: Config,
    events: list[dict[str, Any]],
    response_file: Path,
    viewer_login: str,
    hooks: PRCommentHooks,
) -> bool:
    if not events:
        return False
    pr = events[0]["pr"]
    head_owner = str(pr.get("headRepositoryOwner", {}).get("login") or "")
    head_branch = str(pr.get("headRefName") or "").strip()
    if not head_branch:
        hooks.log(f"跳过 PR#{pr['number']}：缺少 head 分支", module="pr-comment")
        return True
    if head_owner and head_owner != viewer_login:
        hooks.log(
            f"跳过 PR#{pr['number']}：head 属于 {head_owner}，当前用户={viewer_login}",
            module="pr-comment",
        )
        return True
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = cfg.out_dir / f"pr-comment-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    pr_url = str(pr["url"])
    pr_body = str(pr.get("body") or "").strip()
    desc_path = run_dir / "pr-context.md"
    prompt = build_pr_comment_prompt(
        cfg,
        pr_url,
        pr_body,
        events,
    )
    desc_path.write_text(prompt, encoding="utf-8")

    repo_url = cfg.pr_comment_target_repo_url
    hooks.ensure_repo(repo_url, cfg.pr_comment_local_repo)
    hooks.sync_repo(cfg.pr_comment_local_repo)
    hooks.checkout_pr_head(cfg.pr_comment_local_repo, repo_url, head_owner, head_branch)

    write_text(response_file, "running")
    hooks.log(
        f"处理评论：PR#{pr['number']} comments={len(events)} branch={head_branch}",
        module="pr-comment",
    )
    response = hooks.run_claude(
        cfg,
        prompt,
        capture_output=True,
        response_file=response_file,
        repo_dir=cfg.pr_comment_local_repo,
    )
    write_text(response_file, response.strip() or "(empty response)")
    hooks.ensure_committed_and_pushed_to_fork(cfg.pr_comment_local_repo, repo_url)
    resolve_review_threads(cfg, int(pr["number"]), events, hooks)
    hooks.log(
        f"评论处理完成：PR#{pr['number']} Claude返回={truncate_text(response.strip() or '(empty response)')}",
        module="pr-comment",
    )
    return True


def build_pr_comment_prompt(
    cfg: Config,
    pr_url: str,
    pr_body: str,
    events: list[dict[str, Any]],
) -> str:
    rendered_comments: list[str] = []
    for index, event in enumerate(events, start=1):
        comment = event["comment"]
        comment_type = str(event.get("comment_type") or "unknown")
        comment_path = str(comment.get("path") or "").strip()
        comment_line = comment.get("line") or comment.get("original_line")
        comment_url = str(comment.get("html_url") or "")
        comment_body = str(comment.get("body") or "").strip() or "(empty body)"
        meta_lines = [f"{index}. type: {comment_type}"]
        if comment_path:
            meta_lines.append(f"   path: {comment_path}")
        if isinstance(comment_line, int):
            meta_lines.append(f"   line: {comment_line}")
        if comment_url:
            meta_lines.append(f"   url: {comment_url}")
        meta_lines.append("   body:")
        meta_lines.append(f"   {comment_body}")
        rendered_comments.append("\n".join(meta_lines))
    comments_block = "\n\n".join(rendered_comments)
    return (
        f"You are working in the repository https://github.com/{cfg.pr_comment_repo}.git.\n\n"
        f"The PR under discussion is:\n{pr_url}\n\n"
        f"PR description:\n{pr_body or '(empty description)'}\n\n"
        f"New comments to address in this PR batch:\n{comments_block}\n\n"
        "Task:\n"
        "- Read the PR description and all new comments carefully.\n"
        "- If there are review comments on specific files or lines, inspect those areas first.\n"
        "- Make the smallest repository changes necessary to address all comments in this batch.\n"
        "- Work only in the dedicated local review clone, not in the chat-daemon repo.\n"
        "- If comment context references screenshots or local images, assume those images are available locally and can be read directly when needed.\n"
        "- If there is nothing worth changing or no useful repository operation to perform, do not create a git commit.\n"
        "- Create a local git commit if you make repository changes. Do not create a commit when you made no repository changes.\n"
        "- If you create a git commit, push the current branch to the user's fork remote `fork`.\n"
        "- Treat https://github.com/NAOSI-DLUT/dut-manual as the upstream repository for PR review context and main-branch syncing.\n"
        "- Treat https://github.com/LangQi99/dut-manual as the user's fork that should receive pushes.\n"
        "- You may proactively improve nearby structure when it helps address the feedback well: small refactors, clearer organization, new index pages, and lightweight categorization are allowed even for modest comment-driven changes.\n"
        "- Be willing to add small amounts of maintainable scaffolding that make future completion easier, as long as it stays aligned with the repository and the review feedback.\n"
        "- Do not reject a change only because it feels too thin for formal documentation or is supported by only a single concrete data point; these small points are acceptable inputs and are often good seeds for future documentation growth.\n"
        "- Actively consider using these small points to add a small new page, index entry, category, or structural refactor when the feedback suggests a topic that is likely to expand later or gain parallel sibling entries; this kind of early scaffolding is encouraged.\n"
        "- If an existing page is getting too crowded, too mixed in topic, or no longer fits its title well, actively consider splitting it into multiple topic pages instead of continuing to append more content to the same page.\n"
        "- It is encouraged to create a dedicated new page even for only one or two useful sentences when that gives the topic a clearer long-term home, but make sure users can still reach it through clear entry points such as nearby links, index pages, or category pages.\n"
        "- You can also comment to show your insights but must start with [AUTO].\n"
        "- Do not create a PR.\n"
    )


def group_events_by_pr(events: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        pr = event.get("pr") or {}
        pr_number = int(pr.get("number") or 0)
        grouped.setdefault(pr_number, []).append(event)
    return grouped


def select_next_pr_events(pr_events: dict[int, list[dict[str, Any]]]) -> tuple[int, list[dict[str, Any]]]:
    ranked = sorted(
        pr_events.items(),
        key=lambda item: (
            earliest_event_time(item[1]),
            item[0],
        ),
    )
    return ranked[0]


def earliest_event_time(events: list[dict[str, Any]]) -> str:
    values = [
        event_timestamp(event)
        for event in events
        if event_timestamp(event)
    ]
    return min(values) if values else ""


def event_timestamp(event: dict[str, Any]) -> str:
    comment = event.get("comment") or {}
    for key in ("created_at", "submitted_at", "updated_at"):
        value = str(comment.get(key) or "").strip()
        if value:
            return value
    return ""


def mark_events_seen(state: dict[str, Any], events: list[dict[str, Any]]) -> None:
    seen_ids = set(state.setdefault("seen_comment_ids", []))
    for event in events:
        event_id = event_identity(event)
        if event_id not in seen_ids:
            state["seen_comment_ids"].append(event_id)
            seen_ids.add(event_id)


def event_identity(event: dict[str, Any]) -> str:
    comment = event.get("comment") or {}
    comment_type = str(event.get("comment_type") or "")
    if comment_type == "issue_comment":
        return f"issue-comment:{comment['id']}"
    if comment_type == "review":
        return f"review:{comment['id']}"
    if comment_type == "review_comment":
        return f"review-comment:{comment['id']}"
    return f"unknown:{comment.get('id')}"


def resolve_review_threads(
    cfg: Config,
    pr_number: int,
    events: list[dict[str, Any]],
    hooks: PRCommentHooks,
) -> None:
    comment_ids = {
        int((event.get("comment") or {}).get("id"))
        for event in events
        if str(event.get("comment_type") or "") == "review_comment"
        and isinstance((event.get("comment") or {}).get("id"), int)
    }
    if not comment_ids:
        return

    owner, repo = parse_repo_slug(cfg.pr_comment_repo)
    query = """
query($owner: String!, $repo: String!, $prNumber: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $prNumber) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 100) {
            nodes {
              databaseId
            }
          }
        }
      }
    }
  }
}
""".strip()
    try:
        text = hooks.gh_api_text(
            [
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={repo}",
                "-F",
                f"prNumber={pr_number}",
            ],
            cfg.pr_comment_local_repo,
        )
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        hooks.log(f"网页解决失败：PR#{pr_number} 无法读取 review threads: {exc}", module="pr-comment")
        return

    threads = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    thread_ids: list[str] = []
    for thread in threads:
        if not isinstance(thread, dict) or thread.get("isResolved"):
            continue
        comments = ((thread.get("comments") or {}).get("nodes") or [])
        for comment in comments:
            database_id = comment.get("databaseId")
            if isinstance(database_id, int) and database_id in comment_ids:
                thread_id = str(thread.get("id") or "").strip()
                if thread_id:
                    thread_ids.append(thread_id)
                break

    resolved = 0
    mutation = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
""".strip()
    for thread_id in dedupe_preserve(thread_ids):
        try:
            hooks.gh_api_text(
                [
                    "graphql",
                    "-f",
                    f"query={mutation}",
                    "-F",
                    f"threadId={thread_id}",
                ],
                cfg.pr_comment_local_repo,
            )
            resolved += 1
        except Exception as exc:  # noqa: BLE001
            hooks.log(f"网页解决失败：thread={thread_id} err={exc}", module="pr-comment")
    if resolved:
        hooks.log(f"网页解决完成：PR#{pr_number} threads={resolved}", module="pr-comment")


def get_resolved_review_comment_ids(repo: str, pr_number: int) -> set[int]:
    owner, repo_name = parse_repo_slug(repo)
    query = """
query($owner: String!, $repo: String!, $prNumber: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $prNumber) {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          comments(first: 100) {
            nodes {
              databaseId
            }
          }
        }
      }
    }
  }
}
""".strip()
    try:
        result = gh_graphql(
            [
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={repo_name}",
                "-F",
                f"prNumber={pr_number}",
            ]
        )
    except Exception:
        return set()
    nodes = (
        result.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    resolved_ids: set[int] = set()
    for thread in nodes:
        if not isinstance(thread, dict) or not thread.get("isResolved"):
            continue
        comments = ((thread.get("comments") or {}).get("nodes") or [])
        for comment in comments:
            database_id = comment.get("databaseId")
            if isinstance(database_id, int):
                resolved_ids.add(database_id)
    return resolved_ids


def parse_repo_slug(value: str) -> tuple[str, str]:
    owner, repo = value.split("/", 1)
    return owner, repo


def dedupe_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def is_ignorable_comment(comment: dict[str, Any]) -> bool:
    user = comment.get("user") or {}
    login = str(user.get("login") or "")
    if login.endswith("[bot]"):
        return True
    body = str(comment.get("body") or "").strip()
    if body[:6].lower() == "[auto]":
        return True
    if len(body) > MAX_COMMENT_BODY_LENGTH:
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

    result = run_gh_api_with_retry(["gh", "api", path, "--paginate"])
    return json.loads(result.stdout)


def gh_graphql(args: list[str]) -> dict[str, Any]:
    result = run_gh_api_with_retry(["gh", "api", "graphql", *args])
    return json.loads(result.stdout)


def run_gh_api_with_retry(cmd: list[str], attempts: int = 3) -> Any:
    import subprocess

    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return subprocess.run(
                cmd,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            last_exc = exc
            stderr = (exc.stderr or "").strip()
            if attempt >= attempts:
                raise RuntimeError(
                    f"{cmd!r} failed after {attempts} attempts: {stderr or exc}"
                ) from exc
            time.sleep(attempt)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{cmd!r} failed unexpectedly")


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
    existing_seen_ids = list(state.setdefault("seen_comment_ids", []))
    seen_ids = set(existing_seen_ids)
    pending_seen_ids = existing_seen_ids[:]
    for pr in prs:
        pr_number = int(pr["number"])
        issue_comments, review_comments, review_line_comments = fetch_pr_comment_payloads(
            repo,
            pr_number,
        )
        for comment in issue_comments:
            comment_id = f"issue-comment:{comment['id']}"
            if is_ignorable_comment(comment):
                continue
            if comment_id not in seen_ids:
                pending_seen_ids.append(comment_id)
                seen_ids.add(comment_id)
        for review in review_comments:
            review_body = (review.get("body") or "").strip()
            if not review_body:
                continue
            review_id = f"review:{review['id']}"
            if is_ignorable_comment(review):
                continue
            if review_id not in seen_ids:
                pending_seen_ids.append(review_id)
                seen_ids.add(review_id)
        for comment in review_line_comments:
            comment_id = f"review-comment:{comment['id']}"
            if is_ignorable_comment(comment):
                continue
            if comment_id not in seen_ids:
                pending_seen_ids.append(comment_id)
                seen_ids.add(comment_id)
    state["seen_comment_ids"] = pending_seen_ids
    state["initialized"] = True
    log(
        f"首启基线完成：open_pr={len(prs)} seen_comments={len(state['seen_comment_ids'])}",
        module="pr-comment",
    )


def fetch_pr_comment_payloads(
    repo: str,
    pr_number: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    issue_comments = gh_api_json(f"repos/{repo}/issues/{pr_number}/comments")
    review_comments = gh_api_json(f"repos/{repo}/pulls/{pr_number}/reviews")
    review_line_comments = gh_api_json(f"repos/{repo}/pulls/{pr_number}/comments")
    return issue_comments, review_comments, review_line_comments


def truncate_text(text: str, limit: int = 120) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
