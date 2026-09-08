#!/usr/bin/env bash
# Serves the DORA metrics dashboard at http://127.0.0.1:8050, reading the
# CSVs already written to ./data/. Use the "Fetch latest data" button in the
# dashboard, or run `uv run src/collect.py` directly, if ./data/ is empty or
# stale.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

uv sync
uv run src/dashboard.py
