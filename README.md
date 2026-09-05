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

| Variable | Description | Where to get it |
|---|---|---|
| `TRELLO_API_KEY` | Trello API key | https://trello.com/app-key |
| `TRELLO_TOKEN` | Trello API token | Click the "Token" link on https://trello.com/app-key and authorize |
| `TRELLO_BOARD_ID` | Trello board id to read bug cards from | The segment after `/b/` in the board's URL, e.g. `trello.com/b/WEJ9CX5t/...` → `WEJ9CX5t` |
| `GIT_REPO_PATH` | Local path to the git checkout to analyze; also used to derive the GitHub owner/repo (via its `origin` remote) for the lead-time fetch | — |
| `GITHUB_TOKEN` | GitHub token with `actions:read` on the target repo | https://github.com/settings/tokens |
| `WORKFLOW_FILE` | Filename (not path — GitHub Actions always looks in `.github/workflows/`, which isn't configurable) of the workflow to measure pipeline duration for | The `.yml`/`.yaml` file under `.github/workflows/` in the target repo whose runs correspond to a deploy, e.g. `meedoen.yml` |

The easiest way to set these: copy `.env.example` to `.env` and fill in the
values — `fetch_data.sh` loads `.env` automatically if it exists, and will
tell you exactly which variable(s) are still missing (with the hints above)
if you forget one. You can also set them in your shell profile instead;
either way, `.env` is gitignored so it's safe to put real credentials in it.

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
- `lead_time.py` measures the workflow named by `WORKFLOW_FILE` (see above) —
  this is specific to whatever repo `GIT_REPO_PATH` points at, so it needs to
  match that repo's pipeline filename.
