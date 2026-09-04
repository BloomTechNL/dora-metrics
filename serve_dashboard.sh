#!/usr/bin/env bash
# Serves the DORA metrics dashboard at http://127.0.0.1:8050, reading the
# CSVs already written to ./data/ by fetch_data.sh. Run fetch_data.sh first
# if ./data/ is empty or stale.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

uv run src/dashboard.py
