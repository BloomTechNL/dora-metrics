# dora-metrics

Computes four DORA metrics — Mean Time to Recovery, Change Failure Rate,
Deployment Frequency, and Lead Time for Changes — from a Trello board, a
local git checkout, and GitHub Actions run history, and serves them in a
Dash dashboard with configurable time windows and granularity.

Each metric has its own standalone Python script that fetches raw data
and writes it to `data/*.csv` (gitignored). The dashboard reads those CSVs
and does all windowing/aggregation locally — it never talks to Trello, git,
or GitHub itself, so picking a different window or granularity is instant.

## Setup

```bash
uv sync
```

## Usage

```bash
./fetch_data.sh       # populates ./data/*.csv
./serve_dashboard.sh  # serves the dashboard at http://127.0.0.1:8050
```

### `fetch_data.sh`

Runs `uv run src/collect.py`, which runs `src/trello_mttr.py`,
`src/trello_cfr.py`, `src/deploy_frequency.py`, and `src/lead_time.py` in
sequence, writing their output to `data/*.csv`. Takes no arguments.

Required environment variables (the union of what the four scripts need):

| Variable | Description |
|---|---|
| `TRELLO_API_KEY` | Trello API key |
| `TRELLO_TOKEN` | Trello API token |
| `TRELLO_BOARD_ID` | Trello board id to read bug cards from |
| `GIT_REPO_PATH` | Local path to the git checkout to analyze; also used to derive the GitHub owner/repo (via its `origin` remote) for the lead-time fetch |
| `GITHUB_TOKEN` | GitHub token with `actions:read` on the target repo |

Put these in your shell profile or a `.env` file (both are gitignored).

Each underlying script can also be run individually — e.g. `uv run
src/trello_mttr.py` — if you only need to refresh one metric; see its header
comment for the subset of the above variables it actually needs (and, for
`trello_mttr.py`, its `--help`-documented CLI flags).

### `serve_dashboard.sh`

Runs `uv run src/dashboard.py`, serving the dashboard at
`http://127.0.0.1:8050`. Takes no arguments and needs no environment
variables — it only reads the CSVs already in `data/`, so run
`fetch_data.sh` at least once first. Reads: `data/mttr.csv`,
`data/cfr-commits.csv`, `data/cfr-bug-cards.csv`,
`data/deploy-frequency.csv`, `data/lead-time.csv`.

## Notes

- `trello_mttr.py` and `trello_cfr.py` assume the board has "In Progress" and
  "Done" lists and a "bug" label (all overridable via CLI flags on
  `trello_mttr.py`; see its header comment).
- `deploy_frequency.py` and `trello_cfr.py` assume trunk-based development on
  a `main` branch, where every commit is a deploy.
- `lead_time.py` assumes a GitHub Actions workflow at
  `.github/workflows/meedoen.yml` — this is specific to the `BS-F/meedoen`
  repo's pipeline naming and would need updating for a different repo.
