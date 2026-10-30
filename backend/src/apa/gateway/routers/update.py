"""Self-update: check GitHub for new commits, serve client app downloads (APK & EXE),
and apply updates by running deploy.sh when available.

Uses GitHub repo https://github.com/frostfire1/Autonomous-Personal-Assistance.
Endpoints for downloading client binaries (APK and EXE) redirect to GitHub Releases
or serve locally built artifacts if present.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from apa.gateway.routers.ingest import require_ingest_key

router = APIRouter(prefix="/update", tags=["update"])

DEFAULT_REPO = "frostfire1/Autonomous-Personal-Assistance"


def _repo() -> str:
    return os.environ.get("GITHUB_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO


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


def _get_local_sha(root: Path) -> str:
    """Resolve the currently deployed local commit SHA from env, file, or git."""
    for env_var in ("GIT_SHA", "COMMIT_SHA", "APP_VERSION"):
        val = os.environ.get(env_var)
        if val and val.strip():
            return val.strip()[:7]

    for marker in (root / "GIT_SHA", root / ".git_sha", Path("/app/GIT_SHA")):
        if marker.exists():
            try:
                txt = marker.read_text(encoding="utf-8").strip()
                if txt:
                    return txt[:7]
            except Exception:
                pass

    if (root / ".git").exists():
        try:
            res = _git("rev-parse", "--short", "HEAD", cwd=root)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

    return "unknown"


# ── GET /update/check ────────────────────────────────────────────────────────

@router.get("/check", dependencies=[Depends(require_ingest_key)])
async def check_for_updates() -> dict[str, Any]:
    """Check GitHub (https://github.com/frostfire1/Autonomous-Personal-Assistance) for new commits."""
    root = _root()
    repo = _repo()
    repo_url = f"https://github.com/{repo}"
    releases_url = f"https://github.com/{repo}/releases/latest"
    apk_url = f"https://github.com/{repo}/releases/latest/download/apa-latest.apk"
    exe_url = f"https://github.com/{repo}/releases/latest/download/APA-Tracker.exe"

    local_sha = _get_local_sha(root)

    # 1. Fetch latest commit from GitHub API
    remote_sha = "unknown"
    latest = {
        "sha": "",
        "message": "",
        "date": "",
        "author": "",
    }
    behind = 0
    up_to_date = True

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo}/commits/main",
                headers={"User-Agent": "APA-Self-Update"},
            )
            if resp.status_code == 200:
                data = resp.json()
                remote_full = data.get("sha", "")
                remote_sha = remote_full[:7]
                commit_info = data.get("commit", {})
                latest = {
                    "sha": remote_full,
                    "message": commit_info.get("message", "").split("\n")[0],
                    "date": commit_info.get("committer", {}).get("date", ""),
                    "author": commit_info.get("author", {}).get("name", ""),
                }

                if local_sha != "unknown":
                    if local_sha.lower() == remote_sha.lower():
                        up_to_date = True
                        behind = 0
                    else:
                        # Compare commit distance via GitHub API
                        comp_resp = await client.get(
                            f"https://api.github.com/repos/{repo}/compare/{local_sha}...main",
                            headers={"User-Agent": "APA-Self-Update"},
                        )
                        if comp_resp.status_code == 200:
                            comp_data = comp_resp.json()
                            behind = int(comp_data.get("ahead_by", 1))
                            up_to_date = (behind == 0)
                        else:
                            behind = 1
                            up_to_date = False
                else:
                    up_to_date = False
                    behind = 1
            else:
                # GitHub API returned non-200 (e.g. rate limit) -> fallback to git if present
                raise RuntimeError(f"GitHub API status {resp.status_code}")

    except Exception:
        # Fallback to local git if available
        if (root / ".git").exists():
            try:
                _git("fetch", "--quiet", "origin", cwd=root)
                head = _git("rev-parse", "--short", "HEAD", cwd=root)
                local_sha = head.stdout.strip() or local_sha

                remote_branch = "origin/main"
                check = _git("rev-parse", "--verify", "origin/main", cwd=root)
                if check.returncode != 0:
                    remote_branch = "origin/master"

                remote = _git("rev-parse", "--short", remote_branch, cwd=root)
                remote_sha = remote.stdout.strip() or remote_sha

                log = _git("rev-list", "--count", f"HEAD..{remote_branch}", cwd=root)
                behind = int(log.stdout.strip() or "0")
                up_to_date = (behind == 0)

                fmt = _git("log", "-1", "--format=%H%n%s%n%aI%n%an", remote_branch, cwd=root)
                parts = fmt.stdout.strip().splitlines()
                latest = {
                    "sha": parts[0] if len(parts) > 0 else remote_sha,
                    "message": parts[1] if len(parts) > 1 else "",
                    "date": parts[2] if len(parts) > 2 else "",
                    "author": parts[3] if len(parts) > 3 else "",
                }
            except Exception:
                pass

    return {
        "up_to_date": up_to_date,
        "behind": behind,
        "local_sha": local_sha,
        "remote_sha": remote_sha,
        "latest_commit": latest,
        "repo_url": repo_url,
        "releases_url": releases_url,
        "apk_url": apk_url,
        "exe_url": exe_url,
    }


# ── GET /update/download/apk & exe ──────────────────────────────────────────

def _find_local_apk(root: Path) -> Path | None:
    candidates = [
        root / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk",
        root / "android" / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk",
        root / "apa-latest.apk",
        Path("/data/downloads/apa-latest.apk"),
        Path("/data/apa-latest.apk"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def _find_local_exe(root: Path) -> Path | None:
    candidates = [
        root / "tools" / "dist" / "APA-Tracker.exe",
        root / "APA-Tracker.exe",
        Path("/data/downloads/APA-Tracker.exe"),
        Path("/data/APA-Tracker.exe"),
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


@router.get("/download/apk")
async def download_apk() -> Response:
    """Download the Android APK. Serves locally built debug APK or redirects to GitHub Releases."""
    root = _root()
    local_apk = _find_local_apk(root)
    if local_apk is not None:
        return FileResponse(
            str(local_apk),
            media_type="application/vnd.android.package-archive",
            filename="apa-latest.apk",
        )
    repo = _repo()
    return RedirectResponse(
        f"https://github.com/{repo}/releases/latest/download/apa-latest.apk",
        status_code=307,
    )


@router.get("/download/exe")
async def download_exe() -> Response:
    """Download the Windows Desktop Tracker. Serves locally built EXE or redirects to GitHub Releases."""
    root = _root()
    local_exe = _find_local_exe(root)
    if local_exe is not None:
        return FileResponse(
            str(local_exe),
            media_type="application/octet-stream",
            filename="APA-Tracker.exe",
        )
    repo = _repo()
    return RedirectResponse(
        f"https://github.com/{repo}/releases/latest/download/APA-Tracker.exe",
        status_code=307,
    )


@router.get("/downloads")
async def client_downloads() -> dict[str, Any]:
    """Get metadata and download URLs for both Android APK and Windows Tracker EXE."""
    root = _root()
    repo = _repo()
    return {
        "apk": {
            "name": "APA Android App",
            "filename": "apa-latest.apk",
            "url": f"https://github.com/{repo}/releases/latest/download/apa-latest.apk",
            "has_local": _find_local_apk(root) is not None,
            "docs": f"https://github.com/{repo}/blob/main/docs/ANDROID.md",
        },
        "exe": {
            "name": "APA Windows Desktop Tracker",
            "filename": "APA-Tracker.exe",
            "url": f"https://github.com/{repo}/releases/latest/download/APA-Tracker.exe",
            "has_local": _find_local_exe(root) is not None,
            "docs": f"https://github.com/{repo}/blob/main/docs/ACTIVITYWATCH.md",
        },
        "releases_url": f"https://github.com/{repo}/releases/latest",
    }


# ── POST /update/apply ───────────────────────────────────────────────────────

async def _stream_update(root: Path, target: str):
    """Async generator: git pull then deploy.sh <target>, yielding each output line."""
    deploy = root / "deploy" / "deploy.sh"
    repo = _repo()

    if not (root / ".git").exists() or not deploy.exists():
        yield "data: [info] Server is running in container mode (no host git repository mounted).\n\n"
        yield f"data: [info] Official repository: https://github.com/{repo}\n\n"
        yield "data: [deploy] To apply update on your host machine, run:\n\n"
        yield f"data: [deploy]   cd <project_dir> && git pull --rebase && bash deploy/deploy.sh {target}\n\n"
        yield "data: [info] Client apps (APK and EXE) can be downloaded from Releases.\n\n"
        yield "data: [done] 0\n\n"
        return

    # Step 1 – git pull
    yield "data: [git] Pulling latest commits from GitHub…\n\n"
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


@router.post("/apply", dependencies=[Depends(require_ingest_key)])
async def apply_update(target: str = "up") -> StreamingResponse:
    """Pull and redeploy. target='up' rebuilds everything; target='web' rebuilds only the dashboard."""
    root = _root()
    if target not in ("up", "web"):
        raise HTTPException(422, "target must be 'up' or 'web'")

    return StreamingResponse(
        _stream_update(root, target),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
