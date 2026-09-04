#!/usr/bin/env python3
"""
Compute DORA "Lead Time for Changes" from GitHub Actions run history,
operationalized here as pipeline duration: the time from push (workflow
run created_at) to pipeline completion (run updated_at) for the "Meedoen"
workflow (.github/workflows/meedoen.yml) — the pipeline that builds,
tests, and deploys the app. This roughly corresponds to time between push
and deploy, given trunk-based CD. The "Proxy" workflow is ignored.

Only successful runs are counted — a failed/cancelled run doesn't result
in a deploy, so it isn't a push-to-deploy duration.

Uses the batch "list workflow runs" endpoint (up to 100 runs per request,
filtered server-side to status=success and branch=main) rather than
fetching runs one at a time. That endpoint silently caps pagination at
1000 results per query — page 11 comes back empty even when more data
exists — so this recursively splits the created-date range in half
whenever a range's page count saturates the cap, until every leaf range
fits under it. At current volume (~3,300 successful runs) that's under 50
requests total across a handful of ranges, comfortably inside GitHub's
5,000 req/hour authenticated rate limit — no sampling or throttling
needed.

Always writes the raw per-run dataset to data/lead-time.csv — a gitignored
local cache the dashboard reads to recompute duration-per-period over
arbitrary windows without re-hitting the GitHub API.

The GitHub owner/repo is derived from the `origin` remote of the git
checkout at GIT_REPO_PATH, rather than hardcoded — this script targets
whatever repo GIT_REPO_PATH points at.

Usage:
    uv run src/lead_time.py

Required env vars:
    GITHUB_TOKEN
    GIT_REPO_PATH — local path to a git checkout whose "origin" remote
                    points at the GitHub repo to read Actions runs from
"""

import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from lib_dora import DATA_DIR, mean, median, require_env, to_iso, write_csv

GITHUB_API_BASE = "https://api.github.com"
WORKFLOW_FILE = "meedoen.yml"
BRANCH = "main"
CSV_PATH = DATA_DIR / "lead-time.csv"
PAGE_CAP = 10  # GitHub silently stops paginating this endpoint after 1000 results (10 * 100/page)


TRANSIENT_RETRIES = 3


def github_get(path: str, params: dict, token: str):
    url = f"{GITHUB_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    for attempt in range(1, TRANSIENT_RETRIES + 1):
        response = requests.get(url, params=params, headers=headers)
        if response.ok:
            return response.json()
        # Transient 5xx errors show up occasionally under the concurrency this script
        # uses to fetch date-range splits in parallel; a plain retry clears them.
        if response.status_code >= 500 and attempt < TRANSIENT_RETRIES:
            time.sleep(1)
            continue
        raise SystemExit(f"GitHub API error {response.status_code} for {path}: {response.text}")


def get_github_repo_slug(repo_path: str) -> tuple[str, str]:
    remote_url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo_path, capture_output=True, check=True, text=True
    ).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/(.+?)(\.git)?$", remote_url)
    if not match:
        raise SystemExit(f"Could not parse a GitHub owner/repo out of origin remote URL: {remote_url}")
    return match.group(1), match.group(2)


def find_workflow(owner: str, repo: str, token: str) -> dict:
    data = github_get(f"/repos/{owner}/{repo}/actions/workflows", {}, token)
    workflows = data["workflows"]
    workflow = next((w for w in workflows if w["path"] == f".github/workflows/{WORKFLOW_FILE}"), None)
    if workflow is None:
        available = ", ".join(w["path"] for w in workflows)
        raise SystemExit(f"No workflow found with path .github/workflows/{WORKFLOW_FILE}. Available: {available}")
    return {"id": workflow["id"], "created_at": datetime.fromisoformat(workflow["created_at"].replace("Z", "+00:00"))}


def fetch_runs_in_range(
    since: datetime, until: datetime, owner: str, repo: str, workflow_id: int, token: str
) -> list[dict]:
    created = f"{since.isoformat().replace('+00:00', 'Z')}..{until.isoformat().replace('+00:00', 'Z')}"
    runs = []

    for page in range(1, PAGE_CAP + 1):
        data = github_get(
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
            {"status": "success", "branch": BRANCH, "created": created, "per_page": "100", "page": str(page)},
            token,
        )
        workflow_runs = data["workflow_runs"]
        runs.extend(workflow_runs)
        if len(workflow_runs) < 100:
            return runs  # exhausted this range, well under the cap

    # Hit the 1000-result cap for this range: split it in half and recurse, since
    # there may be more runs in here than we were able to see. Fetched concurrently
    # (like the original's Promise.all) since each half can itself split further.
    mid = since + (until - since) / 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        left_future = pool.submit(fetch_runs_in_range, since, mid, owner, repo, workflow_id, token)
        right_future = pool.submit(
            fetch_runs_in_range, mid + timedelta(microseconds=1), until, owner, repo, workflow_id, token
        )
        return left_future.result() + right_future.result()


def get_successful_runs(owner: str, repo: str, workflow_id: int, workflow_created_at: datetime, token: str) -> list[dict]:
    raw_runs = fetch_runs_in_range(workflow_created_at, datetime.now(timezone.utc), owner, repo, workflow_id, token)

    by_id = {run["id"]: run for run in raw_runs}

    runs = []
    for run in by_id.values():
        created_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        completed_at = datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
        runs.append(
            {
                "id": run["id"],
                "created_at": created_at,
                "completed_at": completed_at,
                "duration_minutes": (completed_at - created_at).total_seconds() / 60,
            }
        )
    return runs


def main():
    token = require_env("GITHUB_TOKEN")
    owner, repo = get_github_repo_slug(require_env("GIT_REPO_PATH"))
    workflow = find_workflow(owner, repo, token)
    runs = get_successful_runs(owner, repo, workflow["id"], workflow["created_at"], token)

    if not runs:
        print("No successful pipeline runs found.")
        return

    durations = [r["duration_minutes"] for r in runs]
    print(f"Successful pipeline runs: {len(runs)}")
    print(f"Median pipeline duration: {median(durations):.1f} minutes")
    print(f"(mean: {mean(durations):.1f} min, min: {min(durations):.1f} min, max: {max(durations):.1f} min)")

    rows = [
        [r["id"], to_iso(r["created_at"]), to_iso(r["completed_at"]), f'{r["duration_minutes"]:.2f}']
        for r in runs
    ]
    write_csv(CSV_PATH, ["run_id", "created_at", "completed_at", "duration_minutes"], rows)
    print()
    print(f"Wrote raw data to {CSV_PATH}")


if __name__ == "__main__":
    main()
