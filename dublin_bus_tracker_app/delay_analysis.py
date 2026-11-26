import altair as alt
import pandas as pd
import streamlit as st

PEAK_YELLOW = "#F2C230"  
NORMAL_TEAL = "#4FB3BF"
LINE_GREY = "#8FA3B8"


@st.cache_data(ttl=300, show_spinner=False)
def cached_query(_engine, query):
    """Run the query at most every 5 minutes. With a 1-minute pipeline the
    cloud table holds millions of rows; re-scanning it on every 60-second
    page refresh would slow the dashboard."""
    return pd.read_sql(query, _engine)


def render_delay_analysis_tab(engine):
    st.subheader("Average delay by hour of day")
    st.caption(
        "Mean predicted delay of all stored bus positions, grouped by the hour they were recorded. "
        "Rush hours (7-10 am, 5-8 pm) are marked in yellow. Early buses count as negative."
    )

    query = """
    SELECT
        EXTRACT(HOUR FROM timestamp) AS hour,
        AVG(CASE
                -- numbers only; the optional minus keeps early (negative) delays
                WHEN delay ~ '^-?[0-9.]+$' THEN delay::FLOAT
                ELSE NULL
            END) AS avg_delay,
        COUNT(*) AS positions
    FROM dublin_bus_data
    GROUP BY EXTRACT(HOUR FROM timestamp)
    ORDER BY hour;
    """
    peak_hours = [7, 8, 9, 17, 18, 19]
    hourly_data = cached_query(engine, query)   # cached for 5 minutes

    if hourly_data.empty:
        st.info("No bus data yet. Start the pipeline (dagster dev) to collect some.")
        return

    hourly_data["Period"] = hourly_data["hour"].apply(lambda x: "Rush hour" if x in peak_hours else "Off-peak")
    hourly_data["avg_delay_min"] = hourly_data["avg_delay"] / 60

    x = alt.X("hour:Q", title="Hour of the day", scale=alt.Scale(domain=[0, 23]),
              axis=alt.Axis(format="d", tickCount=12))
    y = alt.Y("avg_delay_min:Q", title="Average delay (minutes)")

    # One continuous line, with points coloured by rush hour / off-peak.
    line = alt.Chart(hourly_data).mark_line(interpolate="monotone", strokeWidth=2.5, color=LINE_GREY).encode(x=x, y=y)
    points = alt.Chart(hourly_data).mark_circle(size=120, opacity=1).encode(
        x=x, y=y,
        color=alt.Color("Period:N", scale=alt.Scale(domain=["Rush hour", "Off-peak"],
                                                    range=[PEAK_YELLOW, NORMAL_TEAL]),
                        legend=alt.Legend(title=None, orient="top-right")),
        tooltip=[alt.Tooltip("hour:Q", title="Hour"),
                 alt.Tooltip("avg_delay_min:Q", title="Avg delay (min)", format=".1f"),
                 alt.Tooltip("positions:Q", title="Bus positions", format=",")],
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=LINE_GREY, opacity=0.5, strokeDash=[4, 4]).encode(y="y:Q")

    chart = (zero + line + points).properties(height=420).configure_axis(
        labelFontSize=12, titleFontSize=13, grid=True, gridOpacity=0.15
    ).configure_view(strokeWidth=0)

    st.altair_chart(chart, width="stretch")

    # Sparse chart
    n_hours = hourly_data["avg_delay"].notna().sum()
    if n_hours < 3:
        st.info(
            f"Only {n_hours} hour(s) of the day have data so far. Keep the pipeline running "
            "(dagster dev with the sensor on) at different times of day to fill in the 24-hour pattern."
        )
