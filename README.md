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

## Required environment variables

| Variable | Used by | Description |
|---|---|---|
| `TRELLO_API_KEY` | `trello_mttr.py`, `trello_cfr.py` | Trello API key |
| `TRELLO_TOKEN` | `trello_mttr.py`, `trello_cfr.py` | Trello API token |
| `TRELLO_BOARD_ID` | `trello_mttr.py`, `trello_cfr.py` | Trello board id to read bug cards from (`trello_mttr.py` also accepts it as a positional arg) |
| `GIT_REPO_PATH` | `trello_cfr.py`, `deploy_frequency.py`, `lead_time.py` | Local path to the git checkout to analyze. `lead_time.py` also derives the GitHub owner/repo from this checkout's `origin` remote |
| `GITHUB_TOKEN` | `lead_time.py` | GitHub token with `actions:read` on the target repo |

Put these in your shell profile or a `.env` file (both are gitignored).

## Usage

```bash
uv run trello_mttr.py
uv run trello_cfr.py
uv run deploy_frequency.py
uv run lead_time.py

uv run dashboard.py
```

or `uv run collect.py` to run all four collection scripts in sequence.

Then open http://127.0.0.1:8050.

## Notes

- `trello_mttr.py` and `trello_cfr.py` assume the board has "In Progress" and
  "Done" lists and a "bug" label (all overridable via CLI flags on
  `trello_mttr.py`; see its header comment).
- `deploy_frequency.py` and `trello_cfr.py` assume trunk-based development on
  a `main` branch, where every commit is a deploy.
- `lead_time.py` assumes a GitHub Actions workflow at
  `.github/workflows/meedoen.yml` — this is specific to the `BS-F/meedoen`
  repo's pipeline naming and would need updating for a different repo.
