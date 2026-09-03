# dora-metrics

Computes four DORA metrics — Mean Time to Recovery, Change Failure Rate,
Deployment Frequency, and Lead Time for Changes — from a Trello board, a
local git checkout, and GitHub Actions run history, and serves them in a
Dash dashboard with configurable time windows and granularity.

Each metric has its own standalone TypeScript script that fetches raw data
and writes it to `data/*.csv` (gitignored). The dashboard reads those CSVs
and does all windowing/aggregation locally — it never talks to Trello, git,
or GitHub itself, so picking a different window or granularity is instant.

## Setup

```bash
nvm use
pnpm install
uv sync
```

## Required environment variables

| Variable | Used by | Description |
|---|---|---|
| `TRELLO_API_KEY` | `trello-mttr.ts`, `trello-cfr.ts` | Trello API key |
| `TRELLO_TOKEN` | `trello-mttr.ts`, `trello-cfr.ts` | Trello API token |
| `TRELLO_BOARD_ID` | `trello-mttr.ts`, `trello-cfr.ts` | Trello board id to read bug cards from (`trello-mttr.ts` also accepts it as a positional arg) |
| `GIT_REPO_PATH` | `trello-cfr.ts`, `deploy-frequency.ts`, `lead-time.ts` | Local path to the git checkout to analyze. `lead-time.ts` also derives the GitHub owner/repo from this checkout's `origin` remote |
| `GITHUB_TOKEN` | `lead-time.ts` | GitHub token with `actions:read` on the target repo |

Put these in your shell profile or a `.env` file (both are gitignored).

## Usage

```bash
node --import tsx/esm trello-mttr.ts
node --import tsx/esm trello-cfr.ts
node --import tsx/esm deploy-frequency.ts
node --import tsx/esm lead-time.ts

uv run dashboard.py
```

or via the package.json scripts: `pnpm run collect` then `pnpm run dashboard`.

Then open http://127.0.0.1:8050.

## Notes

- `trello-mttr.ts` and `trello-cfr.ts` assume the board has "In Progress" and
  "Done" lists and a "bug" label (all overridable via CLI flags on
  `trello-mttr.ts`; see its header comment).
- `deploy-frequency.ts` and `trello-cfr.ts` assume trunk-based development on
  a `main` branch, where every commit is a deploy.
- `lead-time.ts` assumes a GitHub Actions workflow at
  `.github/workflows/meedoen.yml` — this is specific to the `BS-F/meedoen`
  repo's pipeline naming and would need updating for a different repo.
