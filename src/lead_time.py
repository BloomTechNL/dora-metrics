#!/usr/bin/env python3
"""
Compute DORA "Lead Time for Changes" from GitHub Actions run history,
operationalized here as pipeline duration: the time from push (workflow
run created_at) to pipeline completion (run updated_at) for the workflow
at .github/workflows/<WORKFLOW_FILE> — the pipeline that builds, tests, and
deploys the app. This roughly corresponds to time between push and deploy,
given trunk-based CD. Other workflows in the repo (e.g. a "Proxy" workflow)
are ignored. GitHub Actions always looks for workflow files at
.github/workflows/ specifically — that part isn't configurable — so only
the filename varies by project.

Only successful runs are counted — a failed/cancelled run doesn't result
in a deploy, so it isn't a push-to-deploy duration.

Uses the batch "list workflow runs" endpoint (up to 100 runs per request,
filtered server-side to status=success and branch=main) rather than
fetching runs one at a time. That endpoint silently caps pagination at
1000 results per query — page 11 comes back empty even when more data
exists — so this recursively splits the created-date range in half
whenever a range's page count saturates the cap, until every leaf range
fits under it.

Incremental: if data/lead-time.csv already exists, only runs created since
(the newest run_id's created_at already on disk, minus a 24-hour lookback)
are fetched, instead of re-walking the entire history every time — at
current volume (~3,300 successful runs) a full fetch is ~50 requests across
a handful of ranges and takes over a minute, whereas a normal incremental
run only has to look at the last day or two. The 24-hour lookback exists
because a run can still be queued/in-progress (and thus invisible to the
status=success filter) at the moment of a previous fetch; re-checking that
window on every run catches it once it finishes, instead of losing it
forever. Newly-fetched rows are merged into the existing CSV by run id
(overwriting a stale row if a run was somehow re-run), not appended
blindly. If data/lead-time.csv doesn't exist yet, this falls back to a full
fetch from the workflow's creation date, same as before.

Always writes the merged per-run dataset to data/lead-time.csv — a
gitignored local cache the dashboard reads to recompute duration-per-period
over arbitrary windows without re-hitting the GitHub API, and that this
script itself reads on its next run to fetch only what's new.

The GitHub owner/repo is derived from the `origin` remote of the git
checkout at GIT_REPO_PATH, rather than hardcoded — this script targets
whatever repo GIT_REPO_PATH points at.

Usage:
    uv run src/lead_time.py

Required env vars:
    GITHUB_TOKEN
    GIT_REPO_PATH — local path to a git checkout whose "origin" remote
                    points at the GitHub repo to read Actions runs from
    WORKFLOW_FILE — filename (not path) of the workflow under
                    .github/workflows/ to measure, e.g. "meedoen.yml"
"""

import csv
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from lib_dora import DATA_DIR, mean, median, require_env, to_iso, write_csv

GITHUB_API_BASE = "https://api.github.com"
WORKFLOWS_DIR = ".github/workflows"  # the only location GitHub Actions will look for workflow files; not configurable
BRANCH = "main"
CSV_PATH = DATA_DIR / "lead-time.csv"
PAGE_CAP = 10  # GitHub silently stops paginating this endpoint after 1000 results (10 * 100/page)
LOOKBACK_HOURS = 24  # re-check this recent a window every run, to catch runs that were still in-progress last time


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


def find_workflow(owner: str, repo: str, workflow_file: str, token: str) -> dict:
    workflow_path = f"{WORKFLOWS_DIR}/{workflow_file}"
    data = github_get(f"/repos/{owner}/{repo}/actions/workflows", {}, token)
    workflows = data["workflows"]
    workflow = next((w for w in workflows if w["path"] == workflow_path), None)
    if workflow is None:
        available = ", ".join(w["path"] for w in workflows)
        raise SystemExit(f"No workflow found with path {workflow_path}. Available: {available}")
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


def get_successful_runs(
    owner: str, repo: str, workflow_id: int, since: datetime, until: datetime, token: str
) -> list[dict]:
    raw_runs = fetch_runs_in_range(since, until, owner, repo, workflow_id, token)

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


def read_existing_runs() -> dict[int, dict]:
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return {
            int(row["run_id"]): {
                "id": int(row["run_id"]),
                "created_at": datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
                "completed_at": datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00")),
                "duration_minutes": float(row["duration_minutes"]),
            }
            for row in csv.DictReader(f)
        }


def main():
    token = require_env("GITHUB_TOKEN")
    owner, repo = get_github_repo_slug(require_env("GIT_REPO_PATH"))
    workflow_file = require_env("WORKFLOW_FILE")
    workflow = find_workflow(owner, repo, workflow_file, token)

    existing_runs = read_existing_runs()
    if existing_runs:
        newest_known = max(r["created_at"] for r in existing_runs.values())
        since = max(newest_known - timedelta(hours=LOOKBACK_HOURS), workflow["created_at"])
    else:
        since = workflow["created_at"]
    until = datetime.now(timezone.utc)

    fetched = get_successful_runs(owner, repo, workflow["id"], since, until, token)

    if existing_runs:
        new_count = len({r["id"] for r in fetched} - existing_runs.keys())
        print(
            f"Incremental fetch: checked runs created since {since.isoformat()} "
            f"({len(fetched)} found, {new_count} new)."
        )

    merged = {**existing_runs, **{r["id"]: r for r in fetched}}
    runs = sorted(merged.values(), key=lambda r: r["created_at"])

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
