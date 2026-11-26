import pandas as pd
import altair as alt
import requests
import datetime
import os

from zoneinfo import ZoneInfo

ONECALL_4_TIMELINE_1H = "https://api.openweathermap.org/data/4.0/onecall/timeline/1h"
LOCAL_TZ = ZoneInfo("Europe/Dublin")  # bus timestamps are stored in local (Dublin) time

api_call_count = 0

def _get_json(url, params=None):
    """One HTTP call to OpenWeather, counted, with a timeout and a clear error."""
    global api_call_count
    api_call_count += 1
    response = requests.get(url, params=params, timeout=30)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch weather data: {response.status_code}, Response: {response.text[:300]}")
    return response.json()


def fetch_hourly_weather_data(api_key, lat, lon, start_ts, end_ts, max_calls=5):
    """
    Fetch hourly historical weather between two Unix timestamps (UTC seconds)
    using OpenWeather One Call API 4.0, 1-hour timeline.

    The 1h timeline returns up to 20 hourly records per call, plus ready-made
    'prev' / 'next' URLs for the neighbouring pages. We start at start_ts and
    follow those links until the whole window is covered (24 h = about 2 calls).
    max_calls is a safety cap so a bug can never burn through the daily quota.

    Returns a list of dicts: timestamp (naive, Europe/Dublin local time),
    temperature (deg C) and weather_condition (e.g. "Rain", "Clouds").
    """
    first_params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": "metric",
        "start": int(start_ts),
    }
    data = _get_json(ONECALL_4_TIMELINE_1H, first_params)
    if "data" not in data:
        raise Exception(f"Unexpected API response structure: {data}")

    records = {}          # keyed by 'dt' so overlapping pages never create duplicates
    visited = set()

    def collect(page):
        for entry in page.get("data", []):
            dt = entry.get("dt")
            if dt is None or not (start_ts <= dt <= end_ts):
                continue
            weather = entry.get("weather") or [{}]
            records[dt] = {
                "timestamp": datetime.datetime.fromtimestamp(dt, tz=LOCAL_TZ).replace(tzinfo=None),
                "temperature": entry.get("temp"),
                "weather_condition": weather[0].get("main", "Unknown"),
            }

    def page_link(page, key):
        link = page.get(key)
        if not link or link in visited:
            return None
        visited.add(link)
        if "appid=" not in link:  # links are documented as ready-made; add the key just in case
            link += ("&" if "?" in link else "?") + f"appid={api_key}"
        return link

    collect(data)
    page, calls = data, 1
    while calls < max_calls:
        dts = [e["dt"] for e in page.get("data", []) if "dt" in e]
        if not dts:
            break
        if min(dts) > start_ts:            # window starts earlier than this page -> go back
            link = page_link(page, "prev")
        elif max(dts) < end_ts:            # window ends later than this page -> go forward
            link = page_link(page, "next")
        else:
            break                          # this page reaches both ends: done
        if not link:
            break
        page = _get_json(link)
        calls += 1
        collect(page)

    return [records[k] for k in sorted(records)]

def sort_districts(districts):
    def district_key(name):
        if name.startswith("DUBLIN "):
            # Extract the numeric part after "DUBLIN " and convert to int
            try:
                return int(name.split(" ")[1])
            except ValueError:
                return float("inf")  # For cases like "DUBLIN 6W"
        return float("inf")  # Non-Dublin districts go to the end
    return sorted(districts, key=district_key)

DUBLIN_CENTRE = (53.3498, -6.2603)


def district_filter(districts_gdf, key):
    """Pick any number of districts. Empty = all of Dublin.
    Shared by the Live map and Weather impact tabs."""
    import streamlit as st
    options = sort_districts(districts_gdf['Descriptor'].drop_duplicates().tolist())
    return st.multiselect(
        "Districts", options=options, default=[], key=key,
        placeholder="All districts",
        help="Leave empty for all of Dublin, or pick one or more districts.",
    )


def area_label(selected):
    """Readable name for the chosen area, e.g. 'all of Dublin', 'DUBLIN 8',
    'DUBLIN 1 and DUBLIN 8', '5 districts'."""
    if not selected:
        return "all of Dublin"
    if len(selected) == 1:
        return selected[0]
    if len(selected) <= 3:
        return ", ".join(selected[:-1]) + " and " + selected[-1]
    return f"{len(selected)} districts"


def area_centre(districts_gdf, selected):
    """(lat, lon) for weather lookups: the city centre for all of Dublin, otherwise
    the centre of the combined selected districts."""
    if not selected:
        return DUBLIN_CENTRE
    shape = districts_gdf[districts_gdf['Descriptor'].isin(selected)].geometry.unary_union
    return shape.centroid.y, shape.centroid.x


# SQL filter for a LIST of districts. An empty list means all districts.
DISTRICT_SQL = "(cardinality(CAST(:districts AS text[])) = 0 OR district = ANY(CAST(:districts AS text[])))"


def render_weather_impact_tab(engine, districts_gdf):
    import streamlit as st

    st.subheader("Delays compared with the weather at the time")



    if districts_gdf.empty:
        st.error("Districts data is not available. Please check the districts_gdf DataFrame.")
        return

    # Multi-select. Empty = all of Dublin; pick one or several districts.
    selected = district_filter(districts_gdf, key='weather_impact_districts')
    selection_key = tuple(selected)
    if "weather_data" not in st.session_state:
        st.session_state.weather_data = pd.DataFrame()
    if "bus_delay_data" not in st.session_state:
        st.session_state.bus_delay_data = pd.DataFrame()
    if 'last_district' not in st.session_state or st.session_state['last_district'] != selection_key:
        st.session_state.weather_data = pd.DataFrame()  # Reset as empty DataFrame
        st.session_state.bus_delay_data = pd.DataFrame()  # Reset as empty DataFrame
        st.session_state.last_district = selection_key

    # No selection means the whole of Dublin (weather at the city centre, delays
    # from every district).
    place = area_label(selected)
    st.caption(
        f"Compares bus delays in {place} over the last 24 hours with the weather at the time. "
        "Click the button to load the weather (uses the OpenWeather One Call API: about 3 calls)."
    )

    # Initialize session state for weather data
    # Button to fetch weather data
    fetch_weather = st.button("Fetch weather data", key="fetch_weather_button", type="primary")

    if fetch_weather or st.session_state.weather_data is None:  # Only fetch data when the button is clicked
        # Get the centroid of the selected district
        # city centre for all of Dublin, else the centre of the selected districts
        centroid_lat, centroid_lon = area_centre(districts_gdf, selected)

        # Fetch weather data for the selected district
        WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")
        end_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        start_ts = end_ts - 24 * 3600  # the past 24 hours

        try:
            # One Call 4.0: about 2 calls cover 24 hours
            hourly_weather_data = fetch_hourly_weather_data(
                WEATHER_API_KEY, centroid_lat, centroid_lon, start_ts, end_ts
            )
            st.session_state.weather_data = pd.DataFrame(hourly_weather_data)
            st.toast("Weather loaded for the last 24 hours") 
        except Exception as e:
            st.error(f"Error fetching weather data: {e}")
            return
    st.caption(f"OpenWeather calls made this session: {api_call_count}")
    # Fetch bus delay data only if not in session state
    if st.session_state.weather_data.empty:
        st.info("Click **Fetch weather data** above to load the last 24 hours of weather.")
        return

    # SQL query to fetch delay data
    if st.session_state.bus_delay_data.empty:
        # Parameterised query (safe from SQL injection); early (negative) delays included;
        # no selection covers every district.
        from sqlalchemy import text
        query = text("""
        SELECT
            DATE_TRUNC('hour', timestamp) AS hour,
            CASE WHEN delay ~ '^-?[0-9.]+$' THEN delay::FLOAT ELSE NULL END AS delay
        FROM dublin_bus_data
        WHERE """ + DISTRICT_SQL + """
          AND timestamp >= NOW() - INTERVAL '24 HOURS'
        """)
        st.session_state.bus_delay_data = pd.read_sql(query, engine, params={"districts": list(selected)})

    weather_df = st.session_state.weather_data
    bus_delay_data = st.session_state.bus_delay_data

    if not bus_delay_data.empty and not weather_df.empty:
        # Convert timestamps to hourly for both datasets
        bus_delay_data['hour'] = pd.to_datetime(bus_delay_data['hour']).dt.floor('h')
        weather_df['hour'] = pd.to_datetime(weather_df['timestamp']).dt.floor('h')

        # Merge on the hourly timestamps
        merged_data = pd.merge(bus_delay_data, weather_df, on='hour', how='inner')
        if merged_data.empty:
            st.warning(
                "No hours where both bus data and weather exist. Bus data must be from the last "
                "24 hours: keep the pipeline running, then fetch the weather again."
            )
            return

        # Group by weather condition and hour for plotting
        grouped_data = merged_data.groupby(['hour', 'weather_condition']).mean(numeric_only=True).reset_index()
        grouped_data['delay_min'] = grouped_data['delay'] / 60 

        # Create a line chart showing delay trends over time
        line_chart = alt.Chart(grouped_data).mark_line(point=True, strokeWidth=2.5).encode(
            x=alt.X('hour:T', title='Time', axis=alt.Axis(format='%H:%M')),
            y=alt.Y('delay_min:Q', title='Average delay (minutes)'),
            color=alt.Color('weather_condition:N', title='Weather'),
            tooltip=[alt.Tooltip('hour:T', title='Hour', format='%d %b %H:%M'),
                     alt.Tooltip('weather_condition:N', title='Weather'),
                     alt.Tooltip('delay_min:Q', title='Avg delay (min)', format='.1f'),
                     alt.Tooltip('temperature:Q', title='Temp (°C)', format='.0f')]
        ).properties(
            height=420   
        ).configure_axis(
            labelFontSize=12,
            titleFontSize=13,
            grid=True,
            gridOpacity=0.15
        ).configure_view(strokeWidth=0)

        st.altair_chart(line_chart, width='stretch') 
    else:
        st.warning(
            f"No bus delay data for {place} in the last 24 hours. "
            "Keep the pipeline running (dagster dev with the sensor on), then try again."
        )


def fetch_weather_data(lat, lon):
    """Current weather for the Live Map (OpenWeather 2.5)."""
    import streamlit as st
    weather_api_url = "https://api.openweathermap.org/data/2.5/weather"
    weather_api_key = os.getenv("WEATHER_API_KEY")  # Ensure you have this in your .env file
    params = {
        "lat": lat,
        "lon": lon,
        "appid": weather_api_key,
        "units": "metric"  # Use "imperial" for Fahrenheit
    }

    try:
        response = requests.get(weather_api_url, params=params)
        if response.status_code == 200:
            weather_data = response.json()
            return {
                "temperature": weather_data["main"]["temp"],
                "description": weather_data["weather"][0]["description"],
                "humidity": weather_data["main"]["humidity"],
                "wind_speed": weather_data["wind"]["speed"]
            }
        else:
            st.error(f"Failed to fetch weather data: {response.status_code}")
            return None
    except Exception as e:
        st.error(f"An error occurred while fetching weather data: {e}")
        return None
