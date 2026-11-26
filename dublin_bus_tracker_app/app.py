import streamlit as st
import pandas as pd
from sqlalchemy import create_engine
import geopandas as gpd
import pydeck as pdk
import math
import re
from streamlit_autorefresh import st_autorefresh
import os
from dotenv import load_dotenv
import datetime
import json
import urllib.parse
from pathlib import Path
from delay_analysis import render_delay_analysis_tab
from weather_impact import render_weather_impact_tab, district_filter, area_label, area_centre
from weather_impact import fetch_weather_data 
from route_reliability import render_route_reliability_tab

# Wide layout uses the whole screen.
st.set_page_config(
    page_title="Dublin Bus Live Tracker",
    page_icon="🚌",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv(Path(__file__).with_name(".env"), override=True)

def make_engine(url):
    # Name the psycopg2 driver explicitly. Newer SQLAlchemy versions default
    # "postgresql://" to a different driver (psycopg 3) that isn't in requirements.txt.
    if url and url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    # pool_pre_ping: reconnect quietly if a cloud database closed an idle connection
    return create_engine(url, pool_pre_ping=True)


#   BUS_DB_URL  live bus data (table dublin_bus_data), written by the pipeline: AWS RDS
#   DB_URL      bus stops (table bus_stops_with_districts), loaded by Dublin_bus_stops.py
BUS_DB_URL = os.getenv("BUS_DB_URL", "").strip()
STOPS_DB_URL = os.getenv("DB_URL", "").strip()
missing = [name for name, value in (("BUS_DB_URL", BUS_DB_URL), ("DB_URL", STOPS_DB_URL)) if not value]
if missing:
    st.error(f"Missing in dublin_bus_tracker_app/.env: {', '.join(missing)}. "
             "BUS_DB_URL = database with the bus data (AWS RDS); DB_URL = database with the bus stops.")
    st.stop()
bus_data_engine = make_engine(BUS_DB_URL)   # bus positions and delays (AWS RDS)
stops_engine = make_engine(STOPS_DB_URL)    # bus stops (local PostgreSQL)


GEOJSON_PATH = Path(__file__).resolve().parents[1] / "Real_time_geo_locator" / "dublin_postcode_boundaries.geojson"

REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "60"))

# Map background
#   With MAPBOX_API_KEY set in .env: Mapbox style MAP_STYLE (default satellite + street names).
#   Without a key: Streamlit's free default street map (CARTO). A Mapbox style without a
#   key shows a blank background, so the key decides which one is used.
MAPBOX_API_KEY = os.getenv("MAPBOX_API_KEY", "").strip()
MAP_STYLE = os.getenv("MAP_STYLE", "mapbox://styles/mapbox/satellite-streets-v12").strip()


# Satellite photos via Mapbox's Static Tiles API.
# The satellite styles normally load their photos from the "mapbox.satellite" tile service,
# which refuses this token ("429 Too Many Requests"). So the chosen style is downloaded once,
# its satellite source is pointed at the Static Tiles API, and the result is passed to the
# map directly as a data: URL.
SATELLITE_TILES = ("https://api.mapbox.com/styles/v1/mapbox/satellite-v9/tiles/512/{z}/{x}/{y}@2x"
                   "?access_token=")


@st.cache_data(show_spinner=False)
def satellite_style_url(style, token):
    """Return a style URL whose satellite photos come from Static Tiles; MAP_STYLE on any failure."""
    if not style.startswith("mapbox://styles/"):
        return style
    try:
        import requests
        api_url = "https://api.mapbox.com/styles/v1/" + style[len("mapbox://styles/"):]
        resp = requests.get(api_url, params={"access_token": token}, timeout=15)
        resp.raise_for_status()
        style_json = resp.json()
        replaced = 0
        for src in style_json.get("sources", {}).values():
            if src.get("type") == "raster" and src.get("url") == "mapbox://mapbox.satellite":
                src.pop("url")
                src["tiles"] = [SATELLITE_TILES + token]
                src["tileSize"] = 512
                src["attribution"] = "© Mapbox © OpenStreetMap © Maxar"
                replaced += 1
        if not replaced:
            return style                       # not a satellite style: use it as it is
        style_json.pop("projection", None)     # "globe" (v12) isn't supported by Streamlit's map library
        return "data:application/json;charset=utf-8," + urllib.parse.quote(json.dumps(style_json))
    except Exception:
        return style

# Colours used on the map, matching .streamlit/config.toml
BUS_YELLOW = [242, 194, 48]      # Dublin Bus yellow: the one bold colour
STOP_TEAL = [79, 179, 191]
STOP_CYAN = [0, 229, 255]     # Bus stops on the map
DISTRICT_LINE = [143, 163, 184]
STALE_AFTER_MINUTES = 10         # older data than this is flagged in the header
DUBLIN_CENTRE = (53.3498, -6.2603)


def inject_styles():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Overpass:wght@400;600;700;800&display=swap');

        html, body, h1, h2, h3, h4, p, label, li, input, textarea,
        .stMarkdown, [data-testid="stMetricLabel"], [data-testid="stMetricValue"] {
            font-family: 'Overpass', system-ui, sans-serif;
        }
        .block-container { padding-top: 1.6rem; padding-bottom: 2rem; max-width: 1400px; }

        /* Header */
        .app-title { font-size: 2.1rem; font-weight: 800; letter-spacing: -0.01em;
                     margin: 0; line-height: 1.15; }
        .app-status { margin: 0.4rem 0 1.1rem 0; color: #8FA3B8; font-size: 0.98rem; }
        .app-status.stale { color: #F0A35E; }
        .app-status strong { color: #E6EDF3; }

        /* Headline numbers */
        [data-testid="stMetricValue"] { font-weight: 700; font-variant-numeric: tabular-nums; }
        [data-testid="stMetricLabel"] p { font-size: 0.9rem; color: #8FA3B8; }

        /* Tabs: equal width, bold, yellow marker on the active tab.
           [role=tab] matches current Streamlit; data-baseweb matches older versions. */
        .stTabs [role="tablist"] { display: flex; width: 100%; gap: 0;
                                   border-bottom: 1px solid #243B53; }
        .stTabs [role="tab"] { flex: 1 1 0; justify-content: center; text-align: center;
                               min-height: 3.1rem; padding: 0 0.5rem; }
        .stTabs [role="tab"] p { font-size: 1.05rem; font-weight: 700; color: #B9C7D6; margin: 0; }
        .stTabs [role="tab"]:hover p { color: #E6EDF3; }
        .stTabs [role="tab"][aria-selected="true"] p { color: #F2C230; }
        .stTabs .react-aria-SelectionIndicator,
        .stTabs [data-baseweb="tab-highlight"] { background-color: #F2C230; height: 3px; }
        .stTabs [data-baseweb="tab-border"] { display: none; }
        .stTabs [role="tabpanel"] { padding-top: 1.4rem; }
        /* No scroll arrows on the tab bar: tabs shrink and their names wrap,
           so all four always fit. */
        [data-testid="stTabsScrollLeft"], [data-testid="stTabsScrollRight"] { display: none !important; }
        .stTabs [role="tablist"] { overflow: hidden; }
        .stTabs [role="tab"] { min-width: 0; }
        .stTabs [role="tab"] p { white-space: normal; line-height: 1.2; }

        /* Map legend */
        .legend { display: flex; gap: 1.4rem; flex-wrap: wrap; color: #B9C7D6;
                  font-size: 0.92rem; margin-top: 0.6rem; }
        .legend span { display: inline-flex; align-items: center; gap: 0.45rem; }
        .dot { width: 11px; height: 11px; border-radius: 50%; display: inline-block; }
        .dot.bus { background: #F2C230; box-shadow: 0 0 0 2px #0E1E2F, 0 0 0 3px #F2C230; }
        .dot.stop { background: #00E5FF; width: 8px; height: 8px; box-shadow: 0 0 0 1px #FFFFFF; }
        .line { width: 16px; height: 0; border-top: 1.5px solid #8FA3B8; display: inline-block; }

        @media (max-width: 640px) {
            .app-title { font-size: 1.6rem; }
            .stTabs [data-baseweb="tab"] p { font-size: 0.9rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
@st.cache_resource
def fetch_districts_data():
    """District boundaries from the GeoJSON file (Irish grid EPSG:2157 -> GPS EPSG:4326)."""
    return gpd.read_file(GEOJSON_PATH).to_crs(epsg=4326)


def fetch_bus_data():
    has_route = pd.read_sql(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'dublin_bus_data' AND column_name = 'route_id'", bus_data_engine
    ).shape[0] > 0
    route_col = "route_id" if has_route else "NULL AS route_id"
    query = f"""
    SELECT vehicle_id, {route_col}, latitude::float AS latitude, longitude::float AS longitude,
           delay, district, timestamp
    FROM dublin_bus_data
    WHERE timestamp >= (SELECT max(timestamp) FROM dublin_bus_data) - INTERVAL '30 seconds';
    """
    return pd.read_sql(query, bus_data_engine)   # bus data database


def route_number(route_id):
    """Public route/bus number from the NTA route ID.
    The NTA route ID looks like "1_39A_c_a" (or "1 39A c a"): the second part is the
    number passengers see (39A, E2, N4, C1...). Returns None when it isn't available."""
    if not isinstance(route_id, str) or not route_id.strip():
        return None
    parts = re.split(r"[ _]+", route_id.strip())
    return parts[1] if len(parts) >= 2 else parts[0]


def bus_label(row):
    """Tooltip title: 'Bus 39A' when the bus number is known, otherwise the vehicle ID."""
    route = route_number(row.get("route_id"))
    return f"Bus {route}" if route else f"Vehicle {row['vehicle_id']}"


@st.cache_data(ttl=3600)
def _load_bus_stops():
    # Cached for an hour, the stop list doesn't change while the app runs
    query = """
    SELECT busstopid, latitude::float AS latitude, longitude::float AS longitude, district, name
    FROM bus_stops_with_districts;
    """
    return pd.read_sql(query, stops_engine)


STOP_COLUMNS = ["busstopid", "latitude", "longitude", "district", "name"]


def fetch_bus_stops_data():
    """Bus stops from DB_URL. If the stops table hasn't been created in that database yet,
    return no stops (and a message) instead of crashing the whole dashboard.
    Errors are not cached, so the stops appear as soon as the table exists."""
    try:
        return _load_bus_stops(), None
    except Exception as e:
        if "bus_stops_with_districts" in str(e) and "does not exist" in str(e):
            return pd.DataFrame(columns=STOP_COLUMNS), (
                "Bus stops aren't loaded in the database set as DB_URL. Run "
                "`python Real_time_geo_locator/Dublin_bus_stops.py` (with the same DB_URL in "
                "Real_time_geo_locator/.env), then refresh."
            )
        raise


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def delay_seconds(value):
    """Delay as a number of seconds, or None when unknown (e.g. the text 'Unknown/Null')."""
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def format_delay(seconds):
    """Readable delay text, e.g. '32 sec early', '2 min 5 sec late', 'On time'."""
    seconds = delay_seconds(seconds)
    if seconds is None:
        return "No delay data"
    if seconds == 0:
        return "On time"
    minutes, sec = divmod(abs(seconds), 60)
    amount = f"{minutes} min {sec} sec" if minutes else f"{sec} sec"
    return f"{amount} {'early' if seconds < 0 else 'late'}"


def format_age(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "less than a minute old"
    if minutes < 60:
        return f"{minutes} min old"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} old"
    return f"{hours // 24} days old"


# ---------------------------------------------------------------------------
# Header: title, freshness, headline numbers
# ---------------------------------------------------------------------------
def render_header(bus_df):
    st.markdown('<h1 class="app-title">Dublin Bus live tracker</h1>', unsafe_allow_html=True)

    if bus_df.empty:
        st.markdown(
            '<p class="app-status stale">No bus data yet. Start the pipeline '
            '(<strong>dagster dev</strong>) to collect the first snapshot.</p>',
            unsafe_allow_html=True,
        )
        return

    newest = pd.to_datetime(bus_df["timestamp"]).max()
    age = datetime.datetime.now() - newest.to_pydatetime()
    stale = age > datetime.timedelta(minutes=STALE_AFTER_MINUTES)
    when = f"{newest:%d %b %Y, %H:%M}"
    message = (
        f"Latest snapshot <strong>{when}</strong>, {format_age(age)}. "
        + ("Start the pipeline to refresh it." if stale else f"Page refreshes every {REFRESH_SECONDS} s.")
    )
    st.markdown(f'<p class="app-status{" stale" if stale else ""}">{message}</p>', unsafe_allow_html=True)

    delays = bus_df["delay"].map(delay_seconds).dropna()
    avg = delays.mean() if not delays.empty else None
    on_time = (delays.abs() <= 60).mean() * 100 if not delays.empty else None
    very_late = int((delays > 300).sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Buses in Dublin", f"{len(bus_df):,}", border=True,
              help="Buses inside a Dublin postal district in the latest snapshot.")
    if avg is None:
        avg_text = "No data"
    elif abs(avg) < 30:
        avg_text = "On time"
    else:   # short form so it fits the card, e.g. "3.9 min late"
        avg_text = f"{abs(avg) / 60:.1f} min {'early' if avg < 0 else 'late'}"
    c2.metric("Average delay", avg_text, border=True,
              help="Mean predicted delay across buses with delay data. Early buses count as negative.")
    c3.metric("On time", f"{on_time:.0f}%" if on_time is not None else "No data", border=True,
              help="Share of buses within 1 minute of schedule, early or late.")
    c4.metric("More than 5 min late", f"{very_late:,}", border=True,
              help="Buses predicted to arrive over 5 minutes behind schedule.")


# ---------------------------------------------------------------------------
# Live map tab
# ---------------------------------------------------------------------------
def view_for(area_gdf):
    """Centre and zoom so every selected district fits on the map."""
    minx, miny, maxx, maxy = area_gdf.total_bounds
    mid_lat = (miny + maxy) / 2
    map_w, map_h = 1000, 600                                    # approx. map size in pixels
    # deck.gl zoom z shows 512 * 2^z pixels per 360 degrees of longitude; a degree of
    # latitude is 1 / cos(latitude) times taller on the map (about 1.7x in Dublin).
    lat_stretch = 1 / math.cos(math.radians(mid_lat))
    zoom_x = math.log2(map_w * 360 / (512 * max(maxx - minx, 1e-3)))
    zoom_y = math.log2(map_h * 360 / (512 * max(maxy - miny, 1e-3) * lat_stretch))
    zoom = max(9.0, min(13.5, min(zoom_x, zoom_y) - 0.35))     # small margin around the area
    return pdk.ViewState(latitude=mid_lat, longitude=(minx + maxx) / 2, zoom=zoom, pitch=0)


def render_live_map(bus_df, districts_gdf):
    c_district, c_bus, c_stop, c_weather = st.columns([2.4, 1, 1, 1.3], vertical_alignment="bottom")
    with c_district:
        selected = district_filter(districts_gdf, key="map_districts")
    with c_bus:
        show_bus_data = st.toggle("Buses", value=True)
    with c_stop:
        show_stop_data = st.toggle("Bus stops", value=False)
    with c_weather:
        show_weather = st.toggle(
            "Current weather", value=False,
            help="Weather now at the centre of the selected area (Dublin city centre for all districts).",
        )

    all_selected = not selected
    place = area_label(selected)
    stop_df, stops_problem = fetch_bus_stops_data()

    if all_selected:
        lat, lon = DUBLIN_CENTRE
        view_state = pdk.ViewState(latitude=lat, longitude=lon, zoom=10.2, pitch=0)
        area_gdf = districts_gdf
    else:
        bus_df = bus_df[bus_df["district"].isin(selected)]
        stop_df = stop_df[stop_df["district"].isin(selected)]
        area_gdf = districts_gdf[districts_gdf["Descriptor"].isin(selected)]
        view_state = view_for(area_gdf)

    if show_weather:
        lat, lon = area_centre(districts_gdf, selected)
        weather = fetch_weather_data(lat, lon)
        if weather:
            w1, w2, w3, w4 = st.columns(4)
            w1.metric("Weather", weather["description"].capitalize())
            w2.metric("Temperature", f"{weather['temperature']:.0f} °C")
            w3.metric("Humidity", f"{weather['humidity']}%")
            w4.metric("Wind", f"{weather['wind_speed']} m/s")

    # Layers, bottom to top: district outlines, stops, buses
    on_satellite = bool(MAPBOX_API_KEY)
    layers = [pdk.Layer(
        "GeoJsonLayer",
        data=area_gdf,
        # lighter fill and white outlines stay visible on satellite imagery
        get_fill_color="[255, 255, 255, 12]" if on_satellite else "[143, 163, 184, 18]",
        get_line_color=[255, 255, 255, 200] if on_satellite else DISTRICT_LINE + [170],
        line_width_min_pixels=1.5 if on_satellite else 1,
        pickable=False,
    )]

    if show_stop_data and stops_problem:
        st.warning(stops_problem)
    if show_stop_data and not stop_df.empty:
        stop_df = stop_df.copy()
        stop_df["tip_title"] = stop_df["name"].astype(str)
        stop_df["tip_line1"] = "Bus stop " + stop_df["busstopid"].astype(str)
        stop_df["tip_line2"] = stop_df["district"].astype(str)
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=stop_df,
            get_position="[longitude, latitude]",
            get_radius=30,
            radius_min_pixels=3,
            radius_max_pixels=7,
            get_fill_color=STOP_CYAN + [255],
            stroked=True,
            get_line_color=[255, 255, 255, 230],
            line_width_min_pixels=1,
            pickable=True,
            auto_highlight=True,
            highlight_color=[255, 255, 255, 220],
        ))

    if show_bus_data and not bus_df.empty:
        bus_df = bus_df.copy()
        bus_df["tip_title"] = bus_df.apply(bus_label, axis=1)
        bus_df["tip_line1"] = bus_df["delay"].map(format_delay)
        bus_df["tip_line2"] = bus_df.apply(
            lambda r: f"{r['district']} · fleet no. {r['vehicle_id']}", axis=1)
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=bus_df,
            get_position="[longitude, latitude]",
            get_radius=70,
            radius_min_pixels=4.5,
            radius_max_pixels=14,
            get_fill_color=BUS_YELLOW + [240],
            stroked=True,
            get_line_color=[14, 30, 47, 255],
            line_width_min_pixels=1.5,
            pickable=True,
            auto_highlight=True,
            highlight_color=[255, 255, 255, 200],
        ))

    tooltip = {
        "html": "<b>{tip_title}</b><br/>{tip_line1}<br/><span style='opacity:.7'>{tip_line2}</span>",
        "style": {"color": "#E6EDF3", "backgroundColor": "#15283C", "fontSize": "13px",
                  "fontFamily": "Overpass, sans-serif", "padding": "8px 10px",
                  "borderRadius": "6px", "border": "1px solid #243B53"},
    }
    if on_satellite:
        deck = pdk.Deck(
            layers=layers, initial_view_state=view_state, tooltip=tooltip,
            map_provider="mapbox", map_style=satellite_style_url(MAP_STYLE, MAPBOX_API_KEY),
            api_keys={"mapbox": MAPBOX_API_KEY},
        )
    else:
        # free default street map (CARTO), matched to the light/dark theme
        deck = pdk.Deck(layers=layers, initial_view_state=view_state, tooltip=tooltip, map_style=None)
    st.pydeck_chart(deck, height=600)

    n_buses = len(bus_df) if show_bus_data else 0
    n_stops = len(stop_df) if show_stop_data else 0
    st.markdown(
        f"""<div class="legend">
              <span><i class="dot bus"></i>{n_buses:,} bus{'es' if n_buses != 1 else ''}</span>
              <span><i class="dot stop"></i>{n_stops:,} stop{'s' if n_stops != 1 else ''}</span>
              <span><i class="line"></i>{'District boundaries' if len(selected) != 1 else place + ' boundary'}</span>
            </div>""",
        unsafe_allow_html=True,
    )
    if show_bus_data and bus_df.empty:
        st.info(f"No buses in {place} in the latest snapshot.")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
inject_styles()
st_autorefresh(interval=REFRESH_SECONDS * 1000, limit=1000000, key="datarefresh")

districts_gdf = fetch_districts_data()
bus_df = fetch_bus_data()

render_header(bus_df)

tab_map, tab_delay, tab_routes, tab_weather = st.tabs(
    ["Live Map", "Delays by Hour", "Bus Reliability", "Weather Impact"]
)
with tab_map:
    render_live_map(bus_df, districts_gdf)
with tab_delay:
    render_delay_analysis_tab(bus_data_engine)   # bus data database
with tab_routes:
    render_route_reliability_tab(bus_data_engine)   # bus data database
with tab_weather:
    render_weather_impact_tab(bus_data_engine, districts_gdf)   # all districts; bus data database

st.caption(
    f"Bus data: National Transport Authority GTFS-Realtime. Weather: OpenWeather. "
    f"Map: {'© Mapbox, © OpenStreetMap contributors' if MAPBOX_API_KEY else '© OpenStreetMap contributors, © CARTO'}. Page loaded {datetime.datetime.now():%H:%M:%S}."
)
# Shows which map background is in use, so it's easy to check the Mapbox key.
if MAPBOX_API_KEY:
    st.caption(f"Map background: Mapbox `{MAP_STYLE}` (key ending …{MAPBOX_API_KEY[-4:]}).")
else:
    st.caption("Map background: free street map. Add MAPBOX_API_KEY to dublin_bus_tracker_app/.env for satellite.")