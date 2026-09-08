"""
Dash dashboard for four DORA metrics — Mean Time to Recovery, Change Failure
Rate, Deployment Frequency, and Lead Time for Changes — computed from the
CSVs that trello_mttr.py / trello_cfr.py / deploy_frequency.py /
lead_time.py write to ./data/.

Normally this dashboard does not talk to Trello, git, or GitHub itself: it
reads the raw, per-item data those scripts already cached (card timestamps,
commit timestamps, pipeline run timestamps) and does all windowing/
aggregation locally, so picking a different time range or granularity is
instant instead of re-fetching from a rate-limited API. The one exception is
the "Fetch latest data" button, which runs collect.py (the same four
scripts run by `uv run src/collect.py`) in a subprocess and reloads the CSVs
afterwards.

Run:
    ./serve_dashboard.sh

Requires data/mttr.csv, data/cfr-commits.csv, data/cfr-bug-cards.csv,
data/deploy-frequency.csv, and data/lead-time.csv to already exist —
produced by clicking "Fetch latest data" in the dashboard itself, or by
running `uv run src/collect.py` from the repo root.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dash_table, dcc, html

ROOT_DIR = Path(__file__).parent.parent
SRC_DIR = Path(__file__).parent
DATA_DIR = ROOT_DIR / "data"
ENV_PATH = ROOT_DIR / ".env"

COLORS = {
    "page": "#f9f9f7",
    "surface": "#fcfcfb",
    "primary_ink": "#0b0b0b",
    "secondary_ink": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "baseline": "#c3c2b7",
    "border": "rgba(11,11,11,0.10)",
    "mttr": "#2a78d6",
    "cfr": "#eb6834",
    "deploy": "#1baf7a",
    "lead_time": "#eda100",
}

RANGE_OPTIONS = [
    {"label": "Last 30 days", "value": 30},
    {"label": "Last 90 days", "value": 90},
    {"label": "Last 180 days", "value": 180},
    {"label": "Last 365 days", "value": 365},
    {"label": "All time", "value": "all"},
]

GRANULARITY_OPTIONS = [
    {"label": "Monthly", "value": "MS"},
    {"label": "Quarterly", "value": "QS"},
]

LEAD_TIME_GRANULARITY_OPTIONS = [
    {"label": "Daily", "value": "D"},
    {"label": "Weekly", "value": "W"},
    {"label": "Monthly", "value": "MS"},
    {"label": "Quarterly", "value": "QS"},
]

METRIC_INFO = {
    "mttr": (
        'Median time from a "bug"-labelled Trello card first entering "In Progress" '
        'to it last entering "Done", excluding weekends.'
    ),
    "cfr": (
        "Revert-like commits (subject starting with \"revert\") plus \"bug\"-labelled "
        "Trello cards created, divided by total deploys (commits on main), for the period."
    ),
    "deploy": "Number of commits on main — each commit is treated as one deploy.",
    "lead_time": (
        "Median GitHub Actions pipeline duration — time from a workflow run starting "
        "to it completing, across successful runs only."
    ),
}

# Same six variables collect.py requires — see README.md / .env.example.
ENV_FIELDS = [
    {
        "key": "TRELLO_API_KEY",
        "label": "Trello API key",
        "hint": "Get one at https://trello.com/app-key",
    },
    {
        "key": "TRELLO_TOKEN",
        "label": "Trello API token",
        "hint": 'Click the "Token" link on https://trello.com/app-key and authorize',
    },
    {
        "key": "TRELLO_BOARD_ID",
        "label": "Trello board id",
        "hint": "The segment after /b/ in the board's URL, e.g. trello.com/b/WEJ9CX5t/... → WEJ9CX5t",
    },
    {
        "key": "GIT_REPO_PATH",
        "label": "Git repo path",
        "hint": 'Local path to the git checkout to analyze (must have a "main" branch and a GitHub "origin" remote)',
    },
    {
        "key": "GITHUB_TOKEN",
        "label": "GitHub token",
        "hint": 'Personal access token with "actions:read" — https://github.com/settings/tokens',
    },
    {
        "key": "WORKFLOW_FILE",
        "label": "Workflow file",
        "hint": "Filename under .github/workflows/ to measure pipeline duration for, e.g. meedoen.yml",
    },
]


def read_env_file() -> dict:
    if not ENV_PATH.exists():
        return {}
    values = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        try:
            parts = shlex.split(raw_value)
            value = parts[0] if parts else ""
        except ValueError:
            value = raw_value.strip()
        values[key.strip()] = value
    return values


def write_env_file(values: dict) -> None:
    lines = ["# Managed by the dashboard's Settings page — edit here or through the UI."]
    lines += [f"{field['key']}={shlex.quote(values.get(field['key']) or '')}" for field in ENV_FIELDS]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bucket_label(ts: pd.Timestamp, freq: str) -> str:
    if freq == "QS":
        return f"Q{ts.quarter} {ts.year}"
    if freq == "MS":
        return ts.strftime("%b %Y")
    if freq == "W":
        return f"Week of {ts.strftime('%b %d, %Y')}"
    if freq == "D":
        return ts.strftime("%b %d, %Y")
    return ts.strftime("%Y-%m-%d")


CSV_SCHEMAS = {
    "mttr.csv": {
        "card_id": "object",
        "card_name": "object",
        "card_url": "object",
        "in_progress_at": "datetime64[ns]",
        "done_at": "datetime64[ns]",
        "duration_hours": "float64",
    },
    "cfr-commits.csv": {"hash": "object", "date": "datetime64[ns]", "subject": "object", "is_revert": "bool"},
    "cfr-bug-cards.csv": {"card_name": "object", "card_url": "object", "created_at": "datetime64[ns]"},
    "deploy-frequency.csv": {"hash": "object", "date": "datetime64[ns]"},
    "lead-time.csv": {
        "run_id": "int64",
        "created_at": "datetime64[ns]",
        "completed_at": "datetime64[ns]",
        "duration_minutes": "float64",
    },
}


def _read_csv_or_empty(name: str, date_cols: list[str]) -> pd.DataFrame:
    path = DATA_DIR / name
    if not path.exists():
        schema = CSV_SCHEMAS[name]
        return pd.DataFrame({col: pd.Series(dtype=dtype) for col, dtype in schema.items()})
    df = pd.read_csv(path, parse_dates=date_cols)
    for col in date_cols:
        df[col] = df[col].dt.tz_localize(None)
    return df


def load_data():
    mttr = _read_csv_or_empty("mttr.csv", ["in_progress_at", "done_at"])
    commits = _read_csv_or_empty("cfr-commits.csv", ["date"])
    bug_cards = _read_csv_or_empty("cfr-bug-cards.csv", ["created_at"])
    deploys = _read_csv_or_empty("deploy-frequency.csv", ["date"])
    lead_times = _read_csv_or_empty("lead-time.csv", ["created_at", "completed_at"])
    return mttr, commits, bug_cards, deploys, lead_times


MTTR_DF, COMMITS_DF, BUG_CARDS_DF, DEPLOYS_DF, LEAD_TIME_DF = load_data()


def run_fetch() -> tuple[bool, str]:
    """Runs collect.py in a subprocess, using .env values on top of the current
    environment so this works even when the dashboard process itself wasn't
    started with those variables exported."""
    env = {**os.environ, **{k: v for k, v in read_env_file().items() if v}}
    result = subprocess.run(
        [sys.executable, str(SRC_DIR / "collect.py")],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def clip_to_range(df: pd.DataFrame, date_col: str, days) -> pd.DataFrame:
    if days == "all" or days is None:
        return df
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    return df[df[date_col] >= cutoff]


def mttr_summary(days, freq):
    filtered = clip_to_range(MTTR_DF, "done_at", days)
    trend = (
        filtered.set_index("done_at")["duration_hours"]
        .groupby(pd.Grouper(freq=freq))
        .median()
        .dropna()
    )
    overall = filtered["duration_hours"].median()
    return trend, overall, len(filtered)


def cfr_summary(days, freq):
    commits = clip_to_range(COMMITS_DF, "date", days)
    bug_cards = clip_to_range(BUG_CARDS_DF, "created_at", days)

    deploys = commits.groupby(pd.Grouper(key="date", freq=freq)).size()
    reverts = commits[commits["is_revert"]].groupby(pd.Grouper(key="date", freq=freq)).size()
    bug_counts = bug_cards.groupby(pd.Grouper(key="created_at", freq=freq)).size()

    reverts = reverts.reindex(deploys.index, fill_value=0)
    bug_counts = bug_counts.reindex(deploys.index, fill_value=0)
    failures = reverts + bug_counts

    trend = (failures / deploys * 100).where(deploys > 0).dropna()
    overall = (commits["is_revert"].sum() + len(bug_cards)) / len(commits) * 100 if len(commits) else None
    return trend, overall, len(commits)


def deploy_summary(days, freq):
    filtered = clip_to_range(DEPLOYS_DF, "date", days)
    trend = filtered.groupby(pd.Grouper(key="date", freq=freq)).size()

    if days == "all" or days is None:
        span_days = (filtered["date"].max() - filtered["date"].min()).days if len(filtered) else 0
    else:
        span_days = days
    overall = len(filtered) / span_days if span_days > 0 else None
    return trend, overall, len(filtered)


def lead_time_summary(days, freq):
    filtered = clip_to_range(LEAD_TIME_DF, "completed_at", days)
    trend = (
        filtered.set_index("completed_at")["duration_minutes"]
        .groupby(pd.Grouper(freq=freq))
        .median()
        .dropna()
    )
    overall = filtered["duration_minutes"].median()
    return trend, overall, len(filtered)


def apply_chart_theme(fig: go.Figure, y_title: str) -> None:
    fig.update_layout(
        plot_bgcolor=COLORS["surface"],
        paper_bgcolor=COLORS["surface"],
        font=dict(family="system-ui, -apple-system, 'Segoe UI', sans-serif", color=COLORS["secondary_ink"]),
        xaxis=dict(gridcolor=COLORS["grid"], linecolor=COLORS["baseline"], tickfont=dict(color=COLORS["muted"])),
        yaxis=dict(
            title=dict(text=y_title, font=dict(color=COLORS["muted"], size=12)),
            gridcolor=COLORS["grid"],
            linecolor=COLORS["baseline"],
            tickfont=dict(color=COLORS["muted"]),
            rangemode="tozero",
        ),
        hovermode="x unified",
        margin=dict(l=56, r=24, t=16, b=36),
        height=280,
    )


def _no_data_figure() -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text="No data in this window", showarrow=False, font=dict(color=COLORS["muted"], size=13))
    return fig


def line_chart(series: pd.Series, freq: str, color: str, hover_suffix: str, y_title: str) -> go.Figure:
    if len(series) == 0:
        fig = _no_data_figure()
    else:
        fig = go.Figure()
        labels = [bucket_label(idx, freq) for idx in series.index]
        fig.add_trace(
            go.Scatter(
                x=series.index,
                y=series.values,
                customdata=labels,
                mode="lines+markers",
                line=dict(color=color, width=2),
                marker=dict(size=8, color=color),
                hovertemplate=f"%{{customdata}}<br>%{{y:.2f}}{hover_suffix}<extra></extra>",
            )
        )
    apply_chart_theme(fig, y_title)
    return fig


def bar_chart(series: pd.Series, freq: str, color: str, y_title: str) -> go.Figure:
    if len(series) == 0:
        fig = _no_data_figure()
    else:
        fig = go.Figure()
        labels = [bucket_label(idx, freq) for idx in series.index]
        fig.add_trace(
            go.Bar(
                x=series.index,
                y=series.values,
                customdata=labels,
                marker=dict(color=color, cornerradius=4),
                hovertemplate="%{customdata}<br>%{y} deploys<extra></extra>",
            )
        )
    apply_chart_theme(fig, y_title)
    return fig


def build_table(sections: list[tuple[str, pd.Series, str, str]]) -> html.Div:
    """sections: list of (heading, trend series, bucket freq, value column name)."""
    table_style = dict(
        style_as_list_view=True,
        style_header={"fontWeight": "600", "color": COLORS["primary_ink"], "backgroundColor": COLORS["surface"]},
        style_cell={
            "color": COLORS["secondary_ink"],
            "backgroundColor": COLORS["surface"],
            "fontFamily": "system-ui, -apple-system, 'Segoe UI', sans-serif",
            "border": "none",
            "borderBottom": f"1px solid {COLORS['grid']}",
            "padding": "6px 10px",
        },
    )
    children: list = []
    for heading, series, freq, value_col in sections:
        children.append(html.H4(heading, className="table-heading"))
        children.append(
            dash_table.DataTable(
                columns=[{"name": "Period", "id": "bucket"}, {"name": value_col, "id": "value"}],
                data=[{"bucket": bucket_label(idx, freq), "value": round(float(v), 2)} for idx, v in series.items()],
                **table_style,
            )
        )
    return html.Div(children)


def stat_tile(id_: str, label: str) -> html.Div:
    return html.Div(
        [html.Div(label, className="stat-label"), html.Div("—", id=id_, className="stat-value")],
        className="stat-tile",
    )


def chart_card(
    graph_id: str,
    granularity_id: str,
    title: str,
    info_text: str,
    options=GRANULARITY_OPTIONS,
    default: str = "MS",
) -> html.Div:
    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(title, className="chart-title"),
                            html.Span("ⓘ", className="info-icon", title=info_text),
                        ],
                        className="chart-title-row",
                    ),
                    html.Div(
                        [
                            html.Label("Granularity"),
                            dcc.Dropdown(id=granularity_id, options=options, value=default, clearable=False),
                        ],
                        className="chart-control",
                    ),
                ],
                className="chart-header",
            ),
            dcc.Graph(id=graph_id, config={"displayModeBar": False}),
        ],
        className="chart-card",
    )


INDEX_STRING = f"""<!DOCTYPE html>
<html>
    <head>
        {{%metas%}}
        <title>{{%title%}}</title>
        {{%favicon%}}
        {{%css%}}
        <style>
            body {{
                margin: 0;
                background: {COLORS["page"]};
                font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
            }}
            .app-root {{ max-width: 1120px; margin: 0 auto; padding: 32px 24px 64px; }}
            .header h1 {{ margin: 0 0 4px; font-size: 24px; color: {COLORS["primary_ink"]}; }}
            .subtitle {{ margin: 0 0 28px; color: {COLORS["secondary_ink"]}; font-size: 14px; }}
            .controls {{ display: flex; gap: 24px; align-items: flex-end; margin-bottom: 24px; }}
            .control {{ display: flex; flex-direction: column; gap: 4px; min-width: 200px; }}
            .fetch-control {{ min-width: 0; flex-direction: row; align-items: center; gap: 12px; }}
            .fetch-btn {{
                padding: 8px 16px; font-size: 13px; font-weight: 600;
                color: {COLORS["page"]}; background: {COLORS["primary_ink"]}; border: none; border-radius: 6px;
                cursor: pointer;
            }}
            .fetch-btn:hover {{ opacity: 0.85; }}
            .fetch-status {{ font-size: 12px; color: {COLORS["muted"]}; max-width: 420px; }}
            .control label {{
                font-size: 12px; font-weight: 600; color: {COLORS["muted"]};
                text-transform: uppercase; letter-spacing: 0.02em;
            }}
            .stats {{ display: flex; gap: 16px; margin-bottom: 28px; }}
            .stat-tile {{
                flex: 1; background: {COLORS["surface"]}; border: 1px solid {COLORS["border"]};
                border-radius: 8px; padding: 16px 20px;
            }}
            .stat-label {{ font-size: 13px; color: {COLORS["secondary_ink"]}; margin-bottom: 6px; }}
            .stat-value {{ font-size: 28px; font-weight: 600; color: {COLORS["primary_ink"]}; }}
            .chart-grid {{
                display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px;
            }}
            .grid-section-label {{
                grid-column: 1 / -1; font-size: 12px; font-weight: 600; color: {COLORS["muted"]};
                text-transform: uppercase; letter-spacing: 0.04em; margin: 4px 0 -4px;
            }}
            .chart-card {{
                background: {COLORS["surface"]}; border: 1px solid {COLORS["border"]};
                border-radius: 8px; padding: 8px 8px 0;
            }}
            .chart-header {{
                display: flex; align-items: center; justify-content: space-between; padding: 8px 8px 0;
            }}
            .chart-title-row {{ display: flex; align-items: center; gap: 6px; }}
            .chart-title {{ font-size: 15px; font-weight: 700; color: {COLORS["primary_ink"]}; }}
            .info-icon {{
                display: inline-flex; align-items: center; justify-content: center;
                color: {COLORS["muted"]}; font-size: 13px; cursor: default;
            }}
            .chart-control {{ display: flex; flex-direction: column; gap: 4px; width: 160px; }}
            .chart-control label {{
                font-size: 11px; font-weight: 600; color: {COLORS["muted"]};
                text-transform: uppercase; letter-spacing: 0.02em;
            }}
            .table-toggle {{
                background: {COLORS["surface"]}; border: 1px solid {COLORS["border"]};
                border-radius: 8px; padding: 12px 16px; margin-top: 8px;
            }}
            .table-toggle summary {{ cursor: pointer; font-size: 13px; color: {COLORS["secondary_ink"]}; }}
            .table-heading {{ font-size: 13px; color: {COLORS["primary_ink"]}; margin: 16px 0 8px; }}
            .header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }}
            .settings-link {{
                font-size: 22px; line-height: 1; text-decoration: none; color: {COLORS["muted"]}; padding: 4px;
            }}
            .settings-link:hover {{ color: {COLORS["primary_ink"]}; }}
            .back-link {{ font-size: 13px; color: {COLORS["secondary_ink"]}; text-decoration: none; }}
            .back-link:hover {{ color: {COLORS["primary_ink"]}; }}
            .settings-title {{ margin: 12px 0 4px; font-size: 24px; color: {COLORS["primary_ink"]}; }}
            .settings-form {{
                display: flex; flex-direction: column; gap: 18px; max-width: 480px; margin-top: 12px;
            }}
            .settings-field label {{
                display: block; font-size: 12px; font-weight: 600; color: {COLORS["muted"]};
                text-transform: uppercase; letter-spacing: 0.02em; margin-bottom: 4px;
            }}
            .settings-input {{
                width: 100%; box-sizing: border-box; padding: 8px 10px; font-size: 14px;
                border: 1px solid {COLORS["border"]}; border-radius: 6px;
                background: {COLORS["surface"]}; color: {COLORS["primary_ink"]};
            }}
            .settings-hint {{ font-size: 12px; color: {COLORS["muted"]}; margin-top: 4px; }}
            .settings-save-btn {{
                margin-top: 8px; align-self: flex-start; padding: 8px 20px; font-size: 14px; font-weight: 600;
                color: {COLORS["page"]}; background: {COLORS["primary_ink"]}; border: none; border-radius: 6px;
                cursor: pointer;
            }}
            .settings-status {{ margin-top: 12px; font-size: 13px; color: {COLORS["secondary_ink"]}; }}
        </style>
    </head>
    <body>
        {{%app_entry%}}
        <footer>
            {{%config%}}
            {{%scripts%}}
            {{%renderer%}}
        </footer>
    </body>
</html>"""

app = Dash(__name__, suppress_callback_exceptions=True)
app.title = "DORA Metrics"
app.index_string = INDEX_STRING


def dashboard_layout() -> html.Div:
    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.H1("DORA Metrics"),
                            html.P(
                                "Median time to recovery, change failure rate, deployment frequency, and "
                                "lead time for changes, from the Trello board, git history, and GitHub Actions.",
                                className="subtitle",
                            ),
                        ]
                    ),
                    dcc.Link("⚙", href="/settings", className="settings-link", title="Settings"),
                ],
                className="header",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Label("Window"),
                            dcc.Dropdown(id="range-select", options=RANGE_OPTIONS, value="all", clearable=False),
                        ],
                        className="control",
                    ),
                    html.Div(
                        [
                            html.Button("Fetch latest data", id="fetch-button", className="fetch-btn"),
                            html.Div(id="fetch-status", className="fetch-status"),
                        ],
                        className="control fetch-control",
                    ),
                ],
                className="controls",
            ),
            dcc.Loading(
                [
                    html.Div(
                        [
                            stat_tile("mttr-stat", "Median time to recovery"),
                            stat_tile("cfr-stat", "Change failure rate"),
                            stat_tile("deploy-stat", "Deployment frequency"),
                            stat_tile("lead-time-stat", "Median lead time"),
                        ],
                        className="stats",
                    ),
                    html.Div(
                        [
                            html.Div("Stability", className="grid-section-label"),
                            chart_card(
                                "mttr-graph",
                                "mttr-granularity-select",
                                "Median time to recovery",
                                METRIC_INFO["mttr"],
                            ),
                            chart_card(
                                "cfr-graph", "cfr-granularity-select", "Change failure rate", METRIC_INFO["cfr"]
                            ),
                            html.Div("Throughput", className="grid-section-label"),
                            chart_card(
                                "deploy-graph",
                                "deploy-granularity-select",
                                "Deployment frequency",
                                METRIC_INFO["deploy"],
                            ),
                            chart_card(
                                "lead-time-graph",
                                "lead-time-granularity-select",
                                "Median lead time",
                                METRIC_INFO["lead_time"],
                                options=LEAD_TIME_GRANULARITY_OPTIONS,
                                default="W",
                            ),
                        ],
                        className="chart-grid",
                    ),
                    html.Details(
                        [html.Summary("View data as table"), html.Div(id="data-table")], className="table-toggle"
                    ),
                ],
                type="circle",
            ),
        ],
        className="app-root",
    )


def settings_layout() -> html.Div:
    saved = read_env_file()
    fields = [
        html.Div(
            [
                html.Label(field["label"], htmlFor=f"env-{field['key']}"),
                dcc.Input(
                    id=f"env-{field['key']}",
                    type="text",
                    value=saved.get(field["key"], ""),
                    autoComplete="off",
                    className="settings-input",
                ),
                html.Div(field["hint"], className="settings-hint"),
            ],
            className="settings-field",
        )
        for field in ENV_FIELDS
    ]
    return html.Div(
        [
            html.Div(
                [
                    dcc.Link("← Back to dashboard", href="/", className="back-link"),
                    html.H1("Settings", className="settings-title"),
                    html.P(
                        "Saves to .env in the project root. Fill these in, save, then click "
                        '"Fetch latest data" on the dashboard to pull fresh data.',
                        className="subtitle",
                    ),
                ]
            ),
            html.Div(fields, className="settings-form"),
            html.Button("Save", id="settings-save", className="settings-save-btn"),
            html.Div(id="settings-status", className="settings-status"),
        ],
        className="app-root",
    )


app.layout = html.Div([dcc.Location(id="url", refresh=False), html.Div(id="page-content")])


@app.callback(Output("page-content", "children"), Input("url", "pathname"))
def render_page(pathname):
    if pathname == "/settings":
        return settings_layout()
    return dashboard_layout()


@app.callback(
    Output("settings-status", "children"),
    Input("settings-save", "n_clicks"),
    [State(f"env-{field['key']}", "value") for field in ENV_FIELDS],
    prevent_initial_call=True,
)
def save_settings(_n_clicks, *values):
    write_env_file({field["key"]: value for field, value in zip(ENV_FIELDS, values)})
    return 'Saved to .env. Click "Fetch latest data" on the dashboard to fetch fresh data with these settings.'


@app.callback(
    Output("mttr-graph", "figure"),
    Output("cfr-graph", "figure"),
    Output("deploy-graph", "figure"),
    Output("lead-time-graph", "figure"),
    Output("mttr-stat", "children"),
    Output("cfr-stat", "children"),
    Output("deploy-stat", "children"),
    Output("lead-time-stat", "children"),
    Output("data-table", "children"),
    Output("fetch-status", "children"),
    Input("range-select", "value"),
    Input("mttr-granularity-select", "value"),
    Input("cfr-granularity-select", "value"),
    Input("deploy-granularity-select", "value"),
    Input("lead-time-granularity-select", "value"),
    Input("fetch-button", "n_clicks"),
)
def update_dashboard(days, mttr_freq, cfr_freq, deploy_freq, lead_time_freq, _fetch_clicks):
    global MTTR_DF, COMMITS_DF, BUG_CARDS_DF, DEPLOYS_DF, LEAD_TIME_DF

    fetch_status = ""
    if ctx.triggered_id == "fetch-button":
        ok, output = run_fetch()
        MTTR_DF, COMMITS_DF, BUG_CARDS_DF, DEPLOYS_DF, LEAD_TIME_DF = load_data()
        fetch_status = "Fetched latest data." if ok else f"Fetch failed: {output[-300:] or 'see terminal output'}"

    mttr_trend, mttr_overall, _ = mttr_summary(days, mttr_freq)
    cfr_trend, cfr_overall, _ = cfr_summary(days, cfr_freq)
    deploy_trend, deploy_overall, _ = deploy_summary(days, deploy_freq)
    lead_time_trend, lead_time_overall, _ = lead_time_summary(days, lead_time_freq)

    mttr_fig = line_chart(mttr_trend, mttr_freq, COLORS["mttr"], " hours", "Hours")
    cfr_fig = line_chart(cfr_trend, cfr_freq, COLORS["cfr"], "%", "% of deploys")
    deploy_fig = bar_chart(deploy_trend, deploy_freq, COLORS["deploy"], "Deploys")
    lead_time_fig = line_chart(lead_time_trend, lead_time_freq, COLORS["lead_time"], " min", "Minutes")

    mttr_text = f"{mttr_overall:.1f} hours" if pd.notna(mttr_overall) else "No data"
    cfr_text = f"{cfr_overall:.2f}%" if cfr_overall is not None else "No data"
    deploy_text = f"{deploy_overall:.2f} deploys/day" if deploy_overall is not None else "No data"
    lead_time_text = f"{lead_time_overall:.1f} min" if pd.notna(lead_time_overall) else "No data"

    table = build_table(
        [
            ("MTTR by period", mttr_trend, mttr_freq, "Median MTTR (hours)"),
            ("CFR by period", cfr_trend, cfr_freq, "CFR (%)"),
            ("Deploys by period", deploy_trend, deploy_freq, "Deploys"),
            ("Lead time by period", lead_time_trend, lead_time_freq, "Median lead time (min)"),
        ]
    )

    return (
        mttr_fig,
        cfr_fig,
        deploy_fig,
        lead_time_fig,
        mttr_text,
        cfr_text,
        deploy_text,
        lead_time_text,
        table,
        fetch_status,
    )


if __name__ == "__main__":
    app.run(debug=True)
