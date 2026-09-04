#!/usr/bin/env bash
# Fetches raw data for all four DORA metrics (Trello, git, GitHub Actions)
# and writes it to ./data/*.csv. Run this before serve_dashboard.sh, and
# re-run it whenever you want the dashboard to reflect fresh data.
#
# Required env vars: TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID,
# GIT_REPO_PATH, GITHUB_TOKEN — see README.md.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

uv run src/collect.py
