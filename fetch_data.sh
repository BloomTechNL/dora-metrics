#!/usr/bin/env bash
# Fetches raw data for all four DORA metrics (Trello, git, GitHub Actions)
# and writes it to ./data/*.csv. Run this before serve_dashboard.sh, and
# re-run it whenever you want the dashboard to reflect fresh data.
#
# Required env vars: TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID,
# GIT_REPO_PATH, GITHUB_TOKEN, WORKFLOW_FILE — see README.md, or copy
# .env.example to .env and fill it in (this script loads .env automatically
# if present).

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

missing_names=()
missing_hints=()

check_required() {
  local name="$1" hint="$2"
  if [ -z "${!name:-}" ]; then
    missing_names+=("$name")
    missing_hints+=("$hint")
  fi
}

check_required TRELLO_API_KEY "Trello API key — get one at https://trello.com/app-key"
check_required TRELLO_TOKEN "Trello API token — generate it via the link on https://trello.com/app-key"
check_required TRELLO_BOARD_ID "Trello board id — the segment after /b/ in the board's URL"
check_required GIT_REPO_PATH "Local path to the git checkout to analyze, e.g. ~/code/meedoen"
check_required GITHUB_TOKEN "GitHub personal access token with \"actions:read\" — https://github.com/settings/tokens"
check_required WORKFLOW_FILE "Filename of the GitHub Actions workflow to measure pipeline duration for, e.g. meedoen.yml (must live under .github/workflows/ in the target repo)"

if [ "${#missing_names[@]}" -gt 0 ]; then
  echo "Missing required environment variable(s):" >&2
  for i in "${!missing_names[@]}"; do
    echo "  ${missing_names[$i]} — ${missing_hints[$i]}" >&2
  done
  echo "" >&2
  echo "Copy .env.example to .env, fill in the values, and re-run this script." >&2
  exit 1
fi

uv run src/collect.py
