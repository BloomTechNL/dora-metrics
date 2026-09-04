#!/usr/bin/env python3
"""
Compute DORA "Change Failure Rate" from git history + the Trello board.

This team does trunk-based development with continuous deployment: every
commit to `main` is a deploy. So, over the entire git history:

    deploys  = total commits on main
    failures = (bug cards created) + (git revert commits)
    CFR      = failures / deploys

Bug cards approximate "a deploy caused a production issue", even though we
can't trace a bug card back to the exact commit that caused it — this is an
aggregate-rate approximation, not a per-commit causal link. It also means a
single real incident could be double-counted if it produced both a bug card
AND a revert commit fixing it; there's no reliable way to de-duplicate that
here without an explicit link between Trello cards and commits.

Revert commits are detected by a case-insensitive `^revert` subject-line
prefix. This repo's history mixes git's auto-generated `Revert "..."`
messages with hand-written ones ("Reverted logic for...", "revert infra for
now"), so a loose prefix match is used rather than only the strict
`git revert` format — this can occasionally catch a non-product revert
(e.g. "revert to green" for a CI state).

Bug card creation time is derived from the Trello card id itself (same
trick as trello_mttr.py: the first 8 hex chars of a Trello id are a Mongo
ObjectId timestamp). Only non-archived (open) cards are counted, for
consistency with trello_mttr.py.

Usage:
    uv run trello_cfr.py

Always writes the raw (per-commit, per-card) datasets to
data/cfr-commits.csv and data/cfr-bug-cards.csv — gitignored local caches
intended for a dashboard to recompute CFR over arbitrary windows without
re-hitting Trello or re-walking git history.

Required env vars:
    TRELLO_API_KEY
    TRELLO_TOKEN
    TRELLO_BOARD_ID  — the Trello board to read bug cards from
    GIT_REPO_PATH    — local path to the git checkout to read commit history from
"""

import re
import subprocess
from datetime import datetime, timezone

import requests

from lib_dora import DATA_DIR, require_env, to_iso, write_csv

TRELLO_API_BASE = "https://api.trello.com/1"
FIELD_SEP = "\x1f"
LABEL_NAME = "bug"
BRANCH = "main"
REVERT_REGEX = re.compile(r"^revert", re.IGNORECASE)
COMMITS_CSV_PATH = DATA_DIR / "cfr-commits.csv"
BUG_CARDS_CSV_PATH = DATA_DIR / "cfr-bug-cards.csv"


def trello_get(path: str, params: dict, auth: dict):
    url = f"{TRELLO_API_BASE}{path}"
    response = requests.get(url, params={**params, "key": auth["key"], "token": auth["token"]})
    if not response.ok:
        raise SystemExit(f"Trello API error {response.status_code} for {path}: {response.text}")
    return response.json()


def created_at_from_card_id(card_id: str) -> datetime:
    timestamp_hex = card_id[:8]
    return datetime.fromtimestamp(int(timestamp_hex, 16), tz=timezone.utc)


def get_commits(branch: str, cwd: str) -> list[dict]:
    raw = subprocess.run(
        ["git", "log", branch, f"--format=%H{FIELD_SEP}%cI{FIELD_SEP}%s"],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    commits = []
    for line in raw.splitlines():
        if not line:
            continue
        commit_hash, date, subject = line.split(FIELD_SEP)
        commits.append({"hash": commit_hash, "date": datetime.fromisoformat(date), "subject": subject})
    return commits


def format_percent(fraction: float) -> str:
    return f"{fraction * 100:.2f}%"


def main():
    auth = {"key": require_env("TRELLO_API_KEY"), "token": require_env("TRELLO_TOKEN")}
    board_id = require_env("TRELLO_BOARD_ID")
    repo_path = require_env("GIT_REPO_PATH")

    commits = get_commits(BRANCH, repo_path)
    reverts = [c for c in commits if REVERT_REGEX.match(c["subject"])]

    labels = trello_get(f"/boards/{board_id}/labels", {"fields": "id,name"}, auth)
    bug_label = next((l for l in labels if l["name"].strip().lower() == LABEL_NAME), None)
    if bug_label is None:
        available = ", ".join(l["name"] or "(unnamed)" for l in labels)
        raise SystemExit(f'No label named "{LABEL_NAME}" found on board {board_id}. Available labels: {available}')

    all_cards = trello_get(f"/boards/{board_id}/cards/open", {"fields": "id,name,shortUrl,labels"}, auth)
    bug_cards = [
        {"card": c, "created_at": created_at_from_card_id(c["id"])}
        for c in all_cards
        if any(l["id"] == bug_label["id"] for l in c["labels"])
    ]

    deploys = len(commits)
    failures = len(bug_cards) + len(reverts)
    cfr = failures / deploys if deploys > 0 else 0

    print(f"Deploys (commits on {BRANCH}): {deploys}")
    print(f"Bug cards created:              {len(bug_cards)}")
    print(f"Revert commits:                 {len(reverts)}")
    print(f"Failures (bug cards + reverts): {failures}")
    print()
    print(f"Change failure rate: {format_percent(cfr)}")

    print()
    print(f"Revert commits ({len(reverts)}):")
    for r in reverts:
        print(f'  - {r["date"].strftime("%Y-%m-%d")}  {r["hash"][:8]}  {r["subject"]}')

    print()
    print(f"Bug cards ({len(bug_cards)}):")
    for entry in bug_cards:
        card, created_at = entry["card"], entry["created_at"]
        print(f'  - {created_at.strftime("%Y-%m-%d")}  {card["name"]} ({card["shortUrl"]})')

    commits_rows = [
        [c["hash"], to_iso(c["date"]), c["subject"], bool(REVERT_REGEX.match(c["subject"]))] for c in commits
    ]
    write_csv(COMMITS_CSV_PATH, ["hash", "date", "subject", "is_revert"], commits_rows)

    bug_cards_rows = [
        [entry["card"]["name"], entry["card"]["shortUrl"], to_iso(entry["created_at"])] for entry in bug_cards
    ]
    write_csv(BUG_CARDS_CSV_PATH, ["card_name", "card_url", "created_at"], bug_cards_rows)

    print()
    print(f"Wrote raw data to {COMMITS_CSV_PATH} and {BUG_CARDS_CSV_PATH}")


if __name__ == "__main__":
    main()
