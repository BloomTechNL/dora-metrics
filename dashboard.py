"""
Dash dashboard for four DORA metrics — Mean Time to Recovery, Change Failure
Rate, Deployment Frequency, and Lead Time for Changes — computed from the
CSVs that trello_mttr.py / trello_cfr.py / deploy_frequency.py /
lead_time.py write to ./data/.

This dashboard does not talk to Trello, git, or GitHub itself: it reads the
raw, per-item data those scripts already cached (card timestamps, commit
timestamps, pipeline run timestamps) and does all windowing/aggregation
locally, so picking a different time range or granularity is instant instead
of re-fetching from a rate-limited API.

Run:
    uv run dashboard.py

Requires data/mttr.csv, data/cfr-commits.csv, data/cfr-bug-cards.csv,
data/deploy-frequency.csv, and data/lead-time.csv to already exist —
produced by running (from this directory):
    uv run trello_mttr.py
    uv run trello_cfr.py
    uv run deploy_frequency.py
    uv run lead_time.py
"""

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, dash_table, dcc, html

DATA_DIR = Path(__file__).parent / "data"

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


def _require_csv(name: str) -> Path:
    path = DATA_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. From {Path(__file__).parent}, run:\n"
            f"  uv run trello_mttr.py\n"
            f"  uv run trello_cfr.py\n"
            f"  uv run deploy_frequency.py\n"
            f"  uv run lead_time.py\n"
            f"to populate ./data/ first."
        )
    return path


def load_data():
    mttr = pd.read_csv(_require_csv("mttr.csv"), parse_dates=["in_progress_at", "done_at"])
    for col in ("in_progress_at", "done_at"):
        mttr[col] = mttr[col].dt.tz_localize(None)

    commits = pd.read_csv(_require_csv("cfr-commits.csv"), parse_dates=["date"])
    commits["date"] = commits["date"].dt.tz_localize(None)

    bug_cards = pd.read_csv(_require_csv("cfr-bug-cards.csv"), parse_dates=["created_at"])
    bug_cards["created_at"] = bug_cards["created_at"].dt.tz_localize(None)

    deploys = pd.read_csv(_require_csv("deploy-frequency.csv"), parse_dates=["date"])
    deploys["date"] = deploys["date"].dt.tz_localize(None)

    lead_times = pd.read_csv(_require_csv("lead-time.csv"), parse_dates=["created_at", "completed_at"])
    for col in ("created_at", "completed_at"):
        lead_times[col] = lead_times[col].dt.tz_localize(None)

    return mttr, commits, bug_cards, deploys, lead_times


MTTR_DF, COMMITS_DF, BUG_CARDS_DF, DEPLOYS_DF, LEAD_TIME_DF = load_data()


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


def apply_chart_theme(fig: go.Figure, title: str) -> None:
    fig.update_layout(
        title=dict(text=title, font=dict(color=COLORS["primary_ink"], size=16), x=0, xanchor="left"),
        plot_bgcolor=COLORS["surface"],
        paper_bgcolor=COLORS["surface"],
        font=dict(family="system-ui, -apple-system, 'Segoe UI', sans-serif", color=COLORS["secondary_ink"]),
        xaxis=dict(gridcolor=COLORS["grid"], linecolor=COLORS["baseline"], tickfont=dict(color=COLORS["muted"])),
        yaxis=dict(
            gridcolor=COLORS["grid"],
            linecolor=COLORS["baseline"],
            tickfont=dict(color=COLORS["muted"]),
            rangemode="tozero",
        ),
        hovermode="x unified",
        margin=dict(l=48, r=24, t=48, b=36),
        height=280,
    )


def _no_data_figure() -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text="No data in this window", showarrow=False, font=dict(color=COLORS["muted"], size=13))
    return fig


def line_chart(series: pd.Series, freq: str, title: str, color: str, hover_suffix: str) -> go.Figure:
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
    apply_chart_theme(fig, title)
    return fig


def bar_chart(series: pd.Series, freq: str, title: str, color: str) -> go.Figure:
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
    apply_chart_theme(fig, title)
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


def chart_card(graph_id: str, granularity_id: str, options=GRANULARITY_OPTIONS, default: str = "MS") -> html.Div:
    return html.Div(
        [
            html.Div(
                html.Div(
                    [
                        html.Label("Granularity"),
                        dcc.Dropdown(id=granularity_id, options=options, value=default, clearable=False),
                    ],
                    className="chart-control",
                ),
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
            .controls {{ display: flex; gap: 24px; margin-bottom: 24px; }}
            .control {{ display: flex; flex-direction: column; gap: 4px; min-width: 200px; }}
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
            .chart-header {{ display: flex; justify-content: flex-end; padding: 8px 8px 0; }}
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

app = Dash(__name__)
app.title = "DORA Metrics"
app.index_string = INDEX_STRING

app.layout = html.Div(
    [
        html.Div(
            [
                html.H1("DORA Metrics"),
                html.P(
                    "Mean time to recovery, change failure rate, deployment frequency, and "
                    "lead time for changes, from the Trello board, git history, and GitHub Actions.",
                    className="subtitle",
                ),
            ],
            className="header",
        ),
        html.Div(
            [
                html.Div(
                    [html.Label("Window"), dcc.Dropdown(id="range-select", options=RANGE_OPTIONS, value="all", clearable=False)],
                    className="control",
                ),
            ],
            className="controls",
        ),
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
                chart_card("mttr-graph", "mttr-granularity-select"),
                chart_card("cfr-graph", "cfr-granularity-select"),
                html.Div("Velocity", className="grid-section-label"),
                chart_card("deploy-graph", "deploy-granularity-select"),
                chart_card(
                    "lead-time-graph",
                    "lead-time-granularity-select",
                    options=LEAD_TIME_GRANULARITY_OPTIONS,
                    default="D",
                ),
            ],
            className="chart-grid",
        ),
        html.Details([html.Summary("View data as table"), html.Div(id="data-table")], className="table-toggle"),
    ],
    className="app-root",
)


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
    Input("range-select", "value"),
    Input("mttr-granularity-select", "value"),
    Input("cfr-granularity-select", "value"),
    Input("deploy-granularity-select", "value"),
    Input("lead-time-granularity-select", "value"),
)
def update_dashboard(days, mttr_freq, cfr_freq, deploy_freq, lead_time_freq):
    mttr_trend, mttr_overall, _ = mttr_summary(days, mttr_freq)
    cfr_trend, cfr_overall, _ = cfr_summary(days, cfr_freq)
    deploy_trend, deploy_overall, _ = deploy_summary(days, deploy_freq)
    lead_time_trend, lead_time_overall, _ = lead_time_summary(days, lead_time_freq)

    mttr_fig = line_chart(mttr_trend, mttr_freq, "Median time to recovery", COLORS["mttr"], " hours")
    cfr_fig = line_chart(cfr_trend, cfr_freq, "Change failure rate", COLORS["cfr"], "%")
    deploy_fig = bar_chart(deploy_trend, deploy_freq, "Deployment frequency", COLORS["deploy"])
    lead_time_fig = line_chart(lead_time_trend, lead_time_freq, "Median lead time", COLORS["lead_time"], " min")

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
    )


if __name__ == "__main__":
    app.run(debug=True)
