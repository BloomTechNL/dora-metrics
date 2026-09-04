#!/usr/bin/env python3
"""
Compute DORA "Mean Time to Recovery" from a Trello board, reported as the
median (the standard way to summarize MTTR, since duration distributions
are typically right-skewed by a handful of slow outliers) with mean/min/max
shown alongside for reference.

MTTR per card = (last time the card entered "Done") - (first time it ever
entered "In Progress"). Both timestamps come from walking the card's
updateCard:idList action history: "first entered In Progress" is the
earliest move whose listAfter is the In Progress list (counting every
stall/restart, not just the final push), and "last entered Done" is the
most recent move whose listAfter is the Done list.

Only non-archived (open) cards are considered. Cards carrying the target
label that never reached Done, or that reached Done but were never recorded
entering In Progress, are excluded from the MTTR sample and reported
separately. Cards whose In Progress -> Done gap is under 15 minutes (by
default) are also excluded and reported separately: a gap that short is
usually a card being dragged straight through both lists at once (e.g. a
Butler automation, or bulk cleanup) rather than real recovery time.

Usage:
    uv run src/trello_mttr.py [boardId] [--label=bug] [--done=Done] [--in-progress="In Progress"] [--min-duration-minutes=15]

boardId defaults to the TRELLO_BOARD_ID env var if not passed positionally.

Always writes the resolved (raw, per-card) dataset to data/mttr.csv — a
gitignored local cache intended for a dashboard to recompute MTTR over
arbitrary windows without re-hitting Trello.

Required env vars:
    TRELLO_API_KEY
    TRELLO_TOKEN
    TRELLO_BOARD_ID  — the Trello board to read bug cards from (unless passed as an arg)
"""

import argparse
import time
from datetime import datetime

import requests

from lib_dora import DATA_DIR, mean, median, require_env, to_iso, write_csv

TRELLO_API_BASE = "https://api.trello.com/1"
MTTR_CSV_PATH = DATA_DIR / "mttr.csv"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("board_id", nargs="?", default=None)
    parser.add_argument("--label", default="bug")
    parser.add_argument("--done", default="Done")
    parser.add_argument("--in-progress", default="In Progress")
    parser.add_argument("--min-duration-minutes", type=float, default=15)
    args = parser.parse_args()
    return {
        "board_id": args.board_id or require_env("TRELLO_BOARD_ID"),
        "label_name": args.label,
        "done_list_name": args.done,
        "in_progress_list_name": args.in_progress,
        "min_duration_minutes": args.min_duration_minutes,
    }


def find_unique_list(lists: list[dict], name: str, board_id: str) -> dict:
    matches = [l for l in lists if l["name"].strip().lower() == name.strip().lower()]
    if len(matches) == 0:
        raise SystemExit(
            f'No list named "{name}" found on board {board_id}. '
            f'Available lists: {", ".join(l["name"] for l in lists)}'
        )
    if len(matches) > 1:
        joined = ", ".join(f'{l["name"]} ({l["id"]})' for l in matches)
        raise SystemExit(
            f'Multiple lists named "{name}" found; pass an exact name to disambiguate, '
            f"or rename lists. Matches: {joined}"
        )
    return matches[0]


def trello_get(path: str, params: dict, auth: dict):
    url = f"{TRELLO_API_BASE}{path}"
    response = requests.get(url, params={**params, "key": auth["key"], "token": auth["token"]})
    if not response.ok:
        raise SystemExit(f"Trello API error {response.status_code} for {path}: {response.text}")
    return response.json()


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_hours(delta_seconds: float) -> str:
    return f"{delta_seconds / 3600:.2f}"


def main():
    opts = parse_args()
    board_id = opts["board_id"]
    label_name = opts["label_name"]
    done_list_name = opts["done_list_name"]
    in_progress_list_name = opts["in_progress_list_name"]
    min_duration_seconds = opts["min_duration_minutes"] * 60

    auth = {"key": require_env("TRELLO_API_KEY"), "token": require_env("TRELLO_TOKEN")}

    lists = trello_get(f"/boards/{board_id}/lists", {"fields": "id,name"}, auth)
    done_list_id = find_unique_list(lists, done_list_name, board_id)["id"]
    in_progress_list_id = find_unique_list(lists, in_progress_list_name, board_id)["id"]

    labels = trello_get(f"/boards/{board_id}/labels", {"fields": "id,name"}, auth)
    bug_labels = [l for l in labels if l["name"].strip().lower() == label_name.strip().lower()]
    if len(bug_labels) == 0:
        available = ", ".join(l["name"] or "(unnamed)" for l in labels)
        raise SystemExit(f'No label named "{label_name}" found on board {board_id}. Available labels: {available}')
    if len(bug_labels) > 1:
        raise SystemExit(f'Multiple labels named "{label_name}" found; this script can\'t disambiguate labels by name.')
    bug_label_id = bug_labels[0]["id"]

    all_cards = trello_get(
        f"/boards/{board_id}/cards/open", {"fields": "id,name,idList,closed,shortUrl,labels"}, auth
    )
    bug_cards = [c for c in all_cards if any(l["id"] == bug_label_id for l in c["labels"])]
    print(f'Found {len(bug_cards)} card(s) labeled "{label_name}" on board {board_id}. Fetching history...')

    resolved = []
    unresolved = []
    ambiguous = []
    no_in_progress = []
    too_fast = []

    for index, card in enumerate(bug_cards):
        actions = trello_get(
            f'/cards/{card["id"]}/actions',
            {"filter": "updateCard:idList", "limit": "1000", "fields": "type,date,data"},
            auth,
        )

        done_entries = [
            parse_date(a["date"]) for a in actions if a["data"].get("listAfter", {}).get("id") == done_list_id
        ]
        in_progress_entries = [
            parse_date(a["date"]) for a in actions if a["data"].get("listAfter", {}).get("id") == in_progress_list_id
        ]

        if done_entries:
            done_at = max(done_entries)
            if in_progress_entries:
                started_at = min(in_progress_entries)
                duration_seconds = (done_at - started_at).total_seconds()
                entry = {"card": card, "started_at": started_at, "done_at": done_at, "duration_seconds": duration_seconds}
                if duration_seconds < min_duration_seconds:
                    # In Progress -> Done happened almost instantly; likely a card dragged
                    # straight through both lists (automation/bulk cleanup), not real work time.
                    too_fast.append(entry)
                else:
                    resolved.append(entry)
            else:
                # Reached Done but never recorded entering In Progress (e.g. moved directly
                # from another list, or action history has aged out). Can't compute MTTR for it.
                no_in_progress.append(card)
        elif card["idList"] == done_list_id:
            # Currently in Done but no recorded move into it (e.g. created directly there,
            # or action history has aged out of Trello's retention). Can't compute MTTR for it.
            ambiguous.append(card)
        else:
            unresolved.append(card)

        if (index + 1) % 25 == 0:
            print(f"  ...{index + 1}/{len(bug_cards)} processed")
        time.sleep(0.11)  # stay comfortably under Trello's 100 req / 10s rate limit

    print()
    print(
        "Card                                                            In Progress          Reached Done         Hours"
    )
    for r in sorted(resolved, key=lambda r: r["duration_seconds"]):
        name = r["card"]["name"]
        name = name[:57] + "..." if len(name) > 60 else name.ljust(60)
        print(
            f'{name}  {r["started_at"].strftime("%Y-%m-%dT%H:%M")}  '
            f'{r["done_at"].strftime("%Y-%m-%dT%H:%M")}  {format_hours(r["duration_seconds"])}'
        )

    if resolved:
        durations = [r["duration_seconds"] for r in resolved]
        print()
        print(f"Resolved bug cards: {len(resolved)}")
        print(f"Median time to recovery: {format_hours(median(durations))} hours")
        print(
            f"(mean: {format_hours(mean(durations))} hours, min: {format_hours(min(durations))} hours, "
            f"max: {format_hours(max(durations))} hours)"
        )
    else:
        print("No resolved bug cards found — nothing to compute MTTR from.")

    if unresolved:
        print()
        print(f'{len(unresolved)} bug card(s) never reached "{done_list_name}" — excluded from MTTR:')
        for c in unresolved:
            print(f'  - {c["name"]} ({c["shortUrl"]})')
    if ambiguous:
        print()
        print(
            f'{len(ambiguous)} bug card(s) are currently in "{done_list_name}" but have no recorded move '
            f"into it (excluded, can't compute):"
        )
        for c in ambiguous:
            print(f'  - {c["name"]} ({c["shortUrl"]})')
    if no_in_progress:
        print()
        print(
            f'{len(no_in_progress)} bug card(s) reached "{done_list_name}" but never recorded entering '
            f'"{in_progress_list_name}" (excluded, can\'t compute):'
        )
        for c in no_in_progress:
            print(f'  - {c["name"]} ({c["shortUrl"]})')
    if too_fast:
        print()
        print(
            f'{len(too_fast)} bug card(s) went "{in_progress_list_name}" -> "{done_list_name}" in under '
            f'{opts["min_duration_minutes"]} minutes (excluded as likely automation/bulk-move noise):'
        )
        for r in too_fast:
            minutes = r["duration_seconds"] / 60
            print(f'  - {r["card"]["name"]} ({minutes:.1f} min) ({r["card"]["shortUrl"]})')

    rows = [
        [
            r["card"]["name"],
            r["card"]["shortUrl"],
            to_iso(r["started_at"]),
            to_iso(r["done_at"]),
            format_hours(r["duration_seconds"]),
        ]
        for r in resolved
    ]
    write_csv(MTTR_CSV_PATH, ["card_name", "card_url", "in_progress_at", "done_at", "duration_hours"], rows)
    print()
    print(f"Wrote raw data to {MTTR_CSV_PATH}")


if __name__ == "__main__":
    main()
