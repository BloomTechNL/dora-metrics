# dora-metrics

A dashboard for four DORA metrics (kind of, see the dashboard for detail about what it actually measures): Mean Time to Recovery, Change Failure
Rate, Deployment Frequency, and Lead Time for Changes.

## Running it

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you
don't have it already, then from the repo root:

```bash
./serve_dashboard.sh
```
Navigate to `localhost:8050` and click the gear icon (top right of the dashboard) to open Settings and fill
in:

| Field | Where to get it |
|---|---|
| Trello API key | https://trello.com/app-key |
| Trello API token | Click the "Token" link on https://trello.com/app-key and authorize |
| Trello board id | The segment after `/b/` in the board's URL, e.g. `trello.com/b/WEJ9CX5t/...` → `WEJ9CX5t` |
| Git repo path | Local path to the git checkout to analyze |
| GitHub token | A personal access token with `actions:read` — https://github.com/settings/tokens |
| Workflow file | Filename (not path) of the GitHub Actions workflow to measure pipeline duration for |

Save, then click "Fetch latest data" on the dashboard to pull data with
those settings. Re-fetch any time to refresh the charts.

## Assumptions about your workflow
The dashboard makes the following assumptions:
- The target repo lives on GitHub and has one GitHub Actions workflow that
  builds, tests, and deploys it — lead time is that workflow's run
  duration.
- Trunk-based development with continuous deployment: every commit to
  `main` corresponds 1:1 to a deploy.
- Work is tracked on a Trello board with an "In Progress" list and a "Done"
  list, and bugs are marked with a "bug" label — MTTR and Change Failure
  Rate are both derived from those bug cards.
