"""GitHub API access — workflow runs, job logs, issues.

Actions logs arrive as a zip of per-step text files. Downloading the whole
archive for every failure is wasteful, so we pull only the failed jobs' logs and
hand the parser a trimmed window.
"""

from __future__ import annotations

import io
import logging
import zipfile

import httpx

from ..config import settings

log = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    pass


def _headers() -> dict:
    if not settings.github_token:
        raise GitHubError("GITHUB_TOKEN is not configured, so Actions logs cannot be fetched")
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, **kw) -> httpx.Response:
    url = path if path.startswith("http") else f"{settings.github_api}{path}"
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        r = c.get(url, headers=_headers(), **kw)
    if r.status_code == 404:
        raise GitHubError(f"Not found: {url}")
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise GitHubError("GitHub API rate limit exceeded")
    r.raise_for_status()
    return r


def get_run(repo: str, run_id: str | int) -> dict:
    return _get(f"/repos/{repo}/actions/runs/{run_id}").json()


def list_jobs(repo: str, run_id: str | int) -> list[dict]:
    return _get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=50").json().get("jobs", [])


def get_job_log(repo: str, job_id: str | int) -> str:
    try:
        return _get(f"/repos/{repo}/actions/jobs/{job_id}/logs").text
    except Exception as exc:
        log.warning("Could not fetch job log %s: %s", job_id, exc)
        return ""


def get_run_logs(repo: str, run_id: str | int, *, failed_only: bool = True) -> dict[str, str]:
    """Return {job_name: log_text}. Prefers per-job endpoints, falls back to the zip."""
    out: dict[str, str] = {}
    try:
        jobs = list_jobs(repo, run_id)
    except GitHubError:
        jobs = []

    for job in jobs:
        if failed_only and job.get("conclusion") not in ("failure", "cancelled", "timed_out"):
            continue
        text = get_job_log(repo, job["id"])
        if text:
            out[job.get("name", str(job["id"]))] = text

    if out:
        return out

    try:
        resp = _get(f"/repos/{repo}/actions/runs/{run_id}/logs")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if not name.endswith(".txt"):
                    continue
                try:
                    out[name] = zf.read(name).decode("utf-8", errors="ignore")
                except Exception:
                    continue
    except Exception as exc:
        log.warning("Could not fetch run log archive for %s/%s: %s", repo, run_id, exc)
    return out


def failure_annotations(repo: str, run_id: str | int) -> list[dict]:
    """Structured error annotations — often the cleanest statement of what broke."""
    notes: list[dict] = []
    try:
        for job in list_jobs(repo, run_id):
            if job.get("conclusion") != "failure":
                continue
            for step in job.get("steps") or []:
                if step.get("conclusion") == "failure":
                    notes.append({"job": job.get("name"), "step": step.get("name"), "number": step.get("number")})
    except GitHubError:
        pass
    return notes


def create_issue(repo: str, title: str, body: str, labels: list[str] | None = None) -> dict:
    with httpx.Client(timeout=30) as c:
        r = c.post(
            f"{settings.github_api}/repos/{repo}/issues",
            headers=_headers(),
            json={"title": title[:250], "body": body[:60000], "labels": labels or ["viljaops"]},
        )
    r.raise_for_status()
    return r.json()


def recent_commits(repo: str, *, days: int = 14, branch: str = "") -> list[dict]:
    from datetime import timedelta

    from ..models import utcnow

    since = (utcnow() - timedelta(days=days)).isoformat()
    params = {"since": since, "per_page": 100}
    if branch:
        params["sha"] = branch
    try:
        return _get(f"/repos/{repo}/commits", params=params).json()
    except Exception as exc:
        log.warning("Could not list commits for %s: %s", repo, exc)
        return []
