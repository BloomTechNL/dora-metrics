#!/usr/bin/env python3
"""
Compute DORA "Deployment Frequency" from git history.

This team does trunk-based development with continuous deployment: every
commit to `main` is a deploy. So deployment frequency is just how often
commits land on main — no Trello involved, unlike trello_mttr.py /
trello_cfr.py.

Always writes the raw per-commit dataset to data/deploy-frequency.csv — a
gitignored local cache the dashboard reads to recompute deploys-per-period
over arbitrary windows without re-walking git history.

Usage:
    uv run deploy_frequency.py

Required env var:
    GIT_REPO_PATH — local path to the git checkout to read commit history from
"""

import subprocess
from datetime import datetime

from lib_dora import DATA_DIR, require_env, to_iso, write_csv

FIELD_SEP = "\x1f"
BRANCH = "main"
CSV_PATH = DATA_DIR / "deploy-frequency.csv"


def get_commits(branch: str, cwd: str) -> list[dict]:
    raw = subprocess.run(
        ["git", "log", branch, f"--format=%H{FIELD_SEP}%cI"],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    commits = []
    for line in raw.splitlines():
        if not line:
            continue
        commit_hash, date = line.split(FIELD_SEP)
        commits.append({"hash": commit_hash, "date": datetime.fromisoformat(date)})
    return commits


def main():
    commits = get_commits(BRANCH, require_env("GIT_REPO_PATH"))
    sorted_commits = sorted(commits, key=lambda c: c["date"])
    first, last = sorted_commits[0], sorted_commits[-1]
    span_days = (last["date"] - first["date"]).total_seconds() / 86400
    per_day = len(commits) / span_days if span_days > 0 else len(commits)

    print(f"Deploys (commits on {BRANCH}): {len(commits)}")
    print(f'First deploy: {first["date"].strftime("%Y-%m-%d")}')
    print(f'Last deploy:  {last["date"].strftime("%Y-%m-%d")}')
    print(f"Span: {span_days:.1f} days")
    print(f"Deployment frequency: {per_day:.2f} deploys/day")

    rows = [[c["hash"], to_iso(c["date"])] for c in commits]
    write_csv(CSV_PATH, ["hash", "date"], rows)
    print()
    print(f"Wrote raw data to {CSV_PATH}")


if __name__ == "__main__":
    main()
