"""Self-update: check GitHub for new commits and apply them by running deploy.sh.
Same API key as /ingest. Only available when the project folder is a git repo and
deploy/deploy.sh exists next to it — otherwise the endpoints return 409."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from apa.gateway.routers.ingest import require_ingest_key

router = APIRouter(prefix="/update", tags=["update"], dependencies=[Depends(require_ingest_key)])


def _root() -> Path:
    """Project root: dynamically resolved by searching upwards for .git or deploy/deploy.sh."""
    for env_var in ("PROJECT_ROOT", "APA_ROOT", "APP_ROOT"):
        val = os.environ.get(env_var)
        if val and Path(val).exists():
            return Path(val).resolve()
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / ".git").exists() or (parent / "deploy" / "deploy.sh").exists():
            return parent
    if len(p.parents) > 5 and (p.parents[5] / ".git").exists():
        return p.parents[5]
    return p.parents[4] if len(p.parents) > 4 else p.parent


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _require_git(root: Path) -> None:
    if not (root / ".git").exists():
        raise HTTPException(409, "Not a git repository — self-update is not available.")


def _require_deploy(root: Path) -> None:
    if not (root / "deploy" / "deploy.sh").exists():
        raise HTTPException(409, "deploy/deploy.sh not found — self-update is not available.")


# ── GET /update/check ────────────────────────────────────────────────────────

@router.get("/check")
async def check_for_updates() -> dict:
    """Fetch from origin and return how far behind HEAD is, plus the latest remote commit info."""
    root = _root()
    _require_git(root)

    # fetch silently — don't pull, just update refs
    _git("fetch", "--quiet", "origin", cwd=root)

    # current HEAD
    head = _git("rev-parse", "--short", "HEAD", cwd=root)
    head_sha = head.stdout.strip()

    # remote HEAD (origin/main or origin/master, whichever exists)
    remote_branch = "origin/main"
    check = _git("rev-parse", "--verify", "origin/main", cwd=root)
    if check.returncode != 0:
        remote_branch = "origin/master"

    remote = _git("rev-parse", "--short", remote_branch, cwd=root)
    remote_sha = remote.stdout.strip()

    # how many commits ahead is remote?
    log = _git("rev-list", "--count", f"HEAD..{remote_branch}", cwd=root)
    behind = int(log.stdout.strip() or "0")

    # latest commit on remote
    fmt = _git(
        "log", "-1", "--format=%H%n%s%n%aI%n%an", remote_branch, cwd=root
    )
    parts = fmt.stdout.strip().splitlines()
    latest = {
        "sha": parts[0] if len(parts) > 0 else remote_sha,
        "message": parts[1] if len(parts) > 1 else "",
        "date": parts[2] if len(parts) > 2 else "",
        "author": parts[3] if len(parts) > 3 else "",
    }

    # remote URL (for the link)
    remote_url = _git("remote", "get-url", "origin", cwd=root)
    repo_url = remote_url.stdout.strip().rstrip(".git")
    if repo_url.startswith("git@github.com:"):
        repo_url = repo_url.replace("git@github.com:", "https://github.com/")

    return {
        "up_to_date": behind == 0,
        "behind": behind,
        "local_sha": head_sha,
        "remote_sha": remote_sha,
        "latest_commit": latest,
        "repo_url": repo_url,
    }


# ── POST /update/apply ───────────────────────────────────────────────────────

async def _stream_update(root: Path, target: str):
    """Async generator: git pull then deploy.sh <target>, yielding each output line."""
    deploy = root / "deploy" / "deploy.sh"

    # Step 1 – git pull
    yield "data: [git] Pulling latest commits…\n\n"
    proc = await asyncio.create_subprocess_exec(
        "git", "pull", "--rebase",
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ},
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        yield f"data: [git] {line}\n\n"
    await proc.wait()
    if proc.returncode != 0:
        yield f"data: [error] git pull failed (exit {proc.returncode}) — update aborted.\n\n"
        yield "data: [done] 1\n\n"
        return

    # Step 2 – deploy
    yield f"data: [deploy] Running deploy.sh {target}…\n\n"
    proc2 = await asyncio.create_subprocess_exec(
        "bash", str(deploy), target,
        cwd=root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ},
    )
    assert proc2.stdout is not None
    async for raw in proc2.stdout:
        line = raw.decode(errors="replace").rstrip()
        yield f"data: {line}\n\n"
    await proc2.wait()
    code = proc2.returncode
    if code == 0:
        yield "data: [done] 0\n\n"
    else:
        yield f"data: [error] deploy.sh {target} failed (exit {code}).\n\n"
        yield "data: [done] 1\n\n"


@router.post("/apply")
async def apply_update(target: str = "up") -> StreamingResponse:
    """Pull and redeploy.  target='up' rebuilds everything; target='web' rebuilds only the dashboard.
    Returns a text/event-stream of log lines; the final line is 'data: [done] <exit_code>'."""
    root = _root()
    _require_git(root)
    _require_deploy(root)
    if target not in ("up", "web"):
        raise HTTPException(422, "target must be 'up' or 'web'")

    return StreamingResponse(
        _stream_update(root, target),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
