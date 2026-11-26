"""Bus Reliability tab: which buses (Bus 39A, Bus C1...) keep to the timetable, and which don't?

For every route it works out, from the stored bus positions:
  * On-time %      share of checks where the bus was between 1 min early and 5 min late
  * Average delay  mean predicted delay in minutes (negative = running early)
then shows the 10 most reliable and the 10 least reliable routes side by side.

"Delay" is the NTA's predicted arrival delay at the end of the trip. The pipeline checks
every bus once a minute, so a route that runs often has more checks than a quiet one.
"""
import math
import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text

ON_TIME_EARLY_S = 60      # up to 1 minute early still counts as on time
ON_TIME_LATE_S = 300      # up to 5 minutes late still counts as on time
MAX_ABS_DELAY_S = 3 * 3600  # delays beyond ±3 hours are feed errors (e.g. trips past midnight)
MIN_CHECKS = 30           # routes with fewer checks are left out: too little data to judge
TOP_N = 10

GOOD = "#3FB68B"   # green: reliable
BAD = "#E8684A"    # coral: unreliable
TEXT = "#E6EDF3"
MUTED = "#8FA3B8"

PERIODS = {"Last 24 hours": "24 hours", "Last 7 days": "7 days", "All stored data": None}


@st.cache_data(ttl=300, show_spinner=False)
def route_stats(_engine, period):
    """One row per route with on-time %, average delay and sample sizes (cached 5 minutes).
    The period is counted back from the newest stored data, so it still works after the
    pipeline has been stopped."""
    period_sql = ""
    if period:
        period_sql = ("AND timestamp >= (SELECT max(timestamp) FROM dublin_bus_data) "
                      f"- INTERVAL '{period}'")
    query = text(f"""
        WITH checks AS (
            SELECT
                -- NTA route IDs look like "1_39A_c_a" or "1 39A c a":
                -- the 2nd part is the bus number passengers see
                COALESCE(NULLIF((regexp_split_to_array(route_id, '[ _]+'))[2], ''), route_id) AS route,
                trip_id,
                delay::FLOAT AS delay_s
            FROM dublin_bus_data
            WHERE route_id IS NOT NULL AND route_id <> ''
              AND delay ~ '^-?[0-9]+(\\.[0-9]+)?$'
              {period_sql}
        )
        SELECT
            route,
            COUNT(*)                       AS checks,
            COUNT(DISTINCT trip_id)        AS trips,
            AVG(delay_s) / 60.0            AS avg_delay_min,
            100.0 * AVG(CASE WHEN delay_s BETWEEN -{ON_TIME_EARLY_S} AND {ON_TIME_LATE_S}
                             THEN 1 ELSE 0 END) AS on_time_pct,
            100.0 * AVG(CASE WHEN delay_s > {ON_TIME_LATE_S} THEN 1 ELSE 0 END)   AS late_pct,
            100.0 * AVG(CASE WHEN delay_s < -{ON_TIME_EARLY_S} THEN 1 ELSE 0 END) AS early_pct
        FROM checks
        WHERE delay_s BETWEEN -{MAX_ABS_DELAY_S} AND {MAX_ABS_DELAY_S}
        GROUP BY route
    """)
    return pd.read_sql(query, _engine)


def has_route_column(engine):
    query = text("SELECT 1 FROM information_schema.columns "
                 "WHERE table_name = 'dublin_bus_data' AND column_name = 'route_id'")
    return not pd.read_sql(query, engine).empty


def delay_text(minutes):
    if abs(minutes) < 0.05:
        return "on time"
    return f"{abs(minutes):.1f} min {'late' if minutes > 0 else 'early'}"


def rank_routes(df, rank_by):
    """Most reliable first. On-time %: higher is better (ties: delay closer to 0).
    Average delay: closer to the timetable is better, early or late."""
    df = df.assign(abs_delay=df["avg_delay_min"].abs())
    if rank_by == "On-time %":
        return df.sort_values(["on_time_pct", "abs_delay"], ascending=[False, True])
    return df.sort_values(["abs_delay", "on_time_pct"], ascending=[True, False])


def ranking_chart(df, colour, rank_by):
    """Horizontal bars, best/worst at the top, with both numbers written next to each bar."""
    df = df.copy()
    df["Bus"] = "Bus " + df["route"].astype(str)
    df["label"] = df.apply(
        lambda r: f"{r['on_time_pct']:.0f}% on time · {delay_text(r['avg_delay_min'])}", axis=1)
    df["order"] = range(len(df))
    if rank_by == "On-time %":
        x_field, x_title = "on_time_pct", "On-time %"
        x_scale = alt.Scale(domain=[0, 135])   # room for the label after a 100% bar
    else:
        x_field, x_title = "abs_delay", "Average delay (minutes from timetable)"
        x_scale = alt.Scale(domain=[0, max(1.0, float(df["abs_delay"].max()) * 1.6)])

    y = alt.Y("Bus:N", sort=alt.EncodingSortField("order", order="ascending"), title=None,
              axis=alt.Axis(labelFontSize=13, labelFontWeight="bold", labelColor=TEXT,
                            ticks=False, domain=False, labelPadding=8))
    base = alt.Chart(df).encode(
        y=y,
        tooltip=[alt.Tooltip("Bus:N"),
                 alt.Tooltip("on_time_pct:Q", title="On time (%)", format=".1f"),
                 alt.Tooltip("avg_delay_min:Q", title="Average delay (min)", format=".1f"),
                 alt.Tooltip("late_pct:Q", title="More than 5 min late (%)", format=".1f"),
                 alt.Tooltip("early_pct:Q", title="More than 1 min early (%)", format=".1f"),
                 alt.Tooltip("trips:Q", title="Trips", format=","),
                 alt.Tooltip("checks:Q", title="Checks", format=",")],
    )
    bars = base.mark_bar(color=colour, cornerRadiusEnd=4, height=22).encode(
        x=alt.X(f"{x_field}:Q", title=x_title, scale=x_scale,
                axis=alt.Axis(grid=True, gridOpacity=0.12, labelColor=MUTED, titleColor=MUTED,
                              values=[0, 25, 50, 75, 100] if rank_by == "On-time %" else alt.Undefined)))
    labels = base.mark_text(align="left", dx=6, color=TEXT, fontSize=12).encode(
        x=alt.X(f"{x_field}:Q"), text="label:N")
    return (bars + labels).properties(height=max(120, 34 * len(df))).configure_view(strokeWidth=0)


def list_heading(text, colour):
    """Heading for the best/worst lists: a coloured marker plus text, kept on one line with
    the same height on both sides (emoji render at different sizes on different computers)."""
    return (f'<div style="display:flex;align-items:center;gap:10px;height:2.2rem;'
            f'font-size:1.25rem;font-weight:700;white-space:nowrap;overflow:hidden;'
            f'text-overflow:ellipsis;margin-bottom:0.25rem">'
            f'<span style="flex:none;width:14px;height:14px;border-radius:3px;background:{colour}"></span>'
            f'{text}</div>')


def render_route_reliability_tab(engine):
    st.subheader("Which buses run late most often?")
    st.caption(
        f"**On time** = between 1 minute early and 5 minutes late, a common public transport "
        f"standard. **Average delay** = how far the bus is from the timetable on average "
        f"(the NTA's predicted delay at the end of the trip). Every bus is checked once a "
        f"minute; buses with fewer than {MIN_CHECKS} checks are left out. "
        f"**Bus C1** means all C1 buses together, not one vehicle."
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        period_label = st.segmented_control(
            "Period", list(PERIODS), default="Last 7 days", key="rr_period")
    with c2:
        rank_by = st.segmented_control(
            "Rank buses by", ["On-time %", "Average delay"], default="On-time %", key="rr_rank",
            help="On-time %: share of checks within 1 min early to 5 min late. "
                 "Average delay: closest to the timetable is best, whether early or late.")
    period_label = period_label or "Last 7 days"
    rank_by = rank_by or "On-time %"

    if not has_route_column(engine):
        st.info("Bus numbers aren't stored yet. Run the pipeline (dagster dev) once to add them.")
        return

    stats = route_stats(engine, PERIODS[period_label])
    if stats.empty:
        st.info("No bus data with bus numbers in this period yet. Keep the pipeline running.")
        return

    enough = stats[stats["checks"] >= MIN_CHECKS]
    if enough.empty:
        st.info(f"No bus has {MIN_CHECKS} checks yet in this period. "
                "Keep the pipeline running a little longer, or choose a longer period.")
        return

    ranked = rank_routes(enough, rank_by)
    n = len(ranked)
    if n < 2:
        st.info(f"Only Bus {ranked.iloc[0]['route']} has enough data so far. "
                "Keep the pipeline running to compare buses.")
        return
    # With fewer than 20 routes the two lists would overlap, so split them in half instead.
    top_n = min(TOP_N, math.ceil(n / 2))
    best = ranked.head(top_n)
    worst = ranked.tail(n - top_n if n < 2 * TOP_N else TOP_N).iloc[::-1]   # worst first

    # Headline numbers, weighted by checks so busy routes count for more.
    w = enough["checks"]
    network_on_time = (enough["on_time_pct"] * w).sum() / w.sum()
    network_delay = (enough["avg_delay_min"] * w).sum() / w.sum()
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Buses compared", f"{n}", border=True,
              help=f"Bus numbers (39A, C1...) with at least {MIN_CHECKS} checks in this period")
    k2.metric("On time, all buses", f"{network_on_time:.0f}%", border=True)
    k3.metric("Average delay, all buses", delay_text(network_delay), border=True)
    k4.metric("Least reliable bus", f"Bus {worst.iloc[0]['route']}", border=True,
              help=f"{worst.iloc[0]['on_time_pct']:.0f}% on time, "
                   f"{delay_text(worst.iloc[0]['avg_delay_min'])} on average")

    # Headings in their own row and charts in the row below, so both headings sit on the
    # same line and both charts start at the same height on any screen size.
    head_left, head_right = st.columns(2, gap="large")
    head_left.markdown(list_heading(f"{len(best)} most reliable buses", GOOD), unsafe_allow_html=True)
    head_right.markdown(list_heading(f"{len(worst)} least reliable buses", BAD), unsafe_allow_html=True)
    chart_left, chart_right = st.columns(2, gap="large")
    chart_left.altair_chart(ranking_chart(best, GOOD, rank_by), width="stretch")
    chart_right.altair_chart(ranking_chart(worst, BAD, rank_by), width="stretch")

    if n < 2 * TOP_N:
        st.caption(f"Only {n} buses have enough data so far, so they are split between the two lists.")

    with st.expander(f"All {n} buses"):
        table = ranked.reset_index(drop=True)
        table.insert(0, "Rank", range(1, n + 1))
        table["route"] = "Bus " + table["route"].astype(str)
        st.dataframe(
            table[["Rank", "route", "on_time_pct", "avg_delay_min", "late_pct", "early_pct",
                   "trips", "checks"]],
            hide_index=True, width="stretch",
            column_config={
                "route": st.column_config.TextColumn("Bus"),
                "on_time_pct": st.column_config.ProgressColumn(
                    "On time", format="%.0f%%", min_value=0, max_value=100),
                "avg_delay_min": st.column_config.NumberColumn(
                    "Avg delay (min)", format="%.1f", help="Negative = running early"),
                "late_pct": st.column_config.NumberColumn("> 5 min late", format="%.0f%%"),
                "early_pct": st.column_config.NumberColumn("> 1 min early", format="%.0f%%"),
                "trips": st.column_config.NumberColumn("Trips", format="%d"),
                "checks": st.column_config.NumberColumn("Checks", format="%d"),
            },
        )
