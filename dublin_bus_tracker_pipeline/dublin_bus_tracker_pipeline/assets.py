"""
Flow each run:
  fetch_bus_vehicle_data      -> live bus positions from the NTA GTFS-Realtime API
  fetch_bus_trip_delays       -> live delays per trip from the NTA API
  assign_bus_to_district      -> spatial join with the district boundaries file:
                                 which Dublin district is each bus in?
  process_and_store_vehicle_data -> insert Dublin buses into PostgreSQL (AWS RDS), then
                                    apply retention (RETENTION_DAYS / MAX_ROWS)
"""
from dagster import asset, get_dagster_logger, Output
import requests
from google.protobuf.json_format import MessageToJson
from google.transit import gtfs_realtime_pb2
import json
import psycopg2
from psycopg2.extras import execute_values 
import datetime
import os
from pathlib import Path
from dotenv import load_dotenv
import geopandas as gpd
from shapely.geometry import Point

# Load environment variables from the .env
load_dotenv(Path(__file__).with_name(".env"))

# API configuration
vehicle_api_url = os.getenv("VEHICLES_API_URL")          # NTA GTFS-Realtime Vehicles feed
trip_updates_api_url = os.getenv("TRIP_UPDATES_API_URL")  # NTA GTFS-Realtime TripUpdates feed
headers = {
    "x-api-key": os.getenv("API_KEY"),
    "Content-Type": os.getenv("CONTENT_TYPE"),
}


def nta_get(url, retries=2, max_wait=30):
    """GET from the NTA API. If the API answers 429 "Rate limit is exceeded. Try again in N
    seconds", wait N seconds (at most max_wait) and try again, up to `retries` times."""
    import re
    import time
    if not url:
        raise RuntimeError("Set VEHICLES_API_URL and TRIP_UPDATES_API_URL in the pipeline .env file")
    for attempt in range(retries + 1):
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 429 or attempt == retries:
            return response
        match = re.search(r"Try again in (\d+) second", response.text)
        wait = min(max_wait, int(match.group(1)) + 1 if match else 5)
        get_dagster_logger().info(f"NTA rate limit reached; waiting {wait} s and trying again.")
        time.sleep(wait)
    return response

# PostgreSQL connection (AWS RDS), settings from .env
db_params = {
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT"),
    "sslmode": os.getenv("DB_SSLMODE"),
}

# How much bus data to keep, from .env (0 = no limit)
MAX_ROWS = int(os.getenv("MAX_ROWS", 0))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", 0))

PROJECT_ROOT = Path(__file__).resolve().parents[2]
geojson_file_path = PROJECT_ROOT / "Real_time_geo_locator" / "dublin_postcode_boundaries.geojson"


def load_districts_from_file():
    """Read district polygons from the local GeoJSON file.
    The file is stored in Irish grid coordinates (EPSG:2157), so convert to
    GPS latitude/longitude (EPSG:4326) to match the bus positions."""
    return gpd.read_file(geojson_file_path).to_crs(epsg=4326)


def real_vehicle_id(entity):
    """The bus's own ID from the feed.
    entity["id"] (e.g. "V35") is only a row label inside ONE download of the feed and can
    point to a different bus in the next download (GTFS-Realtime spec: "unique within a
    FeedMessage"). The vehicle's ID is in vehicle.vehicle.id (or its public label).
    Falls back to the row label only if the feed gives neither."""
    descriptor = entity.get("vehicle", {}).get("vehicle", {})
    return str(descriptor.get("id") or descriptor.get("label") or entity["id"])


@asset
def fetch_bus_vehicle_data():
    """Fetch live vehicle positions from the NTA API (GTFS-Realtime protobuf)."""
    response = nta_get(vehicle_api_url)  # waits and retries if rate-limited
    if response.status_code == 200:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)
        entity_data = json.loads(MessageToJson(feed)).get("entity", [])
        return Output(value=entity_data, metadata={"entity_count": len(entity_data)})
    raise Exception(f"API Error: {response.status_code} {response.text[:200]}")


def trip_delay(stop_time_updates):
    """Predicted delay in seconds for a trip. Uses the arrival delay at the last stop
    that has one. If no stop has an arrival delay (typically a bus still waiting at its first
    stop, where the feed only says how late it will DEPART), it uses the latest departure delay.
    Returns "Unknown" only when the feed gives no delay at all."""
    for kind in ("arrival", "departure"):
        for stop in reversed(stop_time_updates):
            delay = (stop.get(kind) or {}).get("delay")
            if delay is not None:
                return delay
    return "Unknown"


@asset(non_argument_deps={"fetch_bus_vehicle_data"})
def fetch_bus_trip_delays():
    """Fetch trip delays from the NTA API: returns {trip_id: delay_seconds}."""
    response = nta_get(trip_updates_api_url)  # waits and retries if rate-limited
    delay_mapping = {}
    if response.status_code == 200:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)
        trip_updates = json.loads(MessageToJson(feed)).get("entity", [])
        for entity in trip_updates:
            if "tripUpdate" in entity and "trip" in entity["tripUpdate"]:
                trip_id = entity["tripUpdate"]["trip"].get("tripId")
                if trip_id:
                    stop_time_updates = entity["tripUpdate"].get("stopTimeUpdate", [])
                    if stop_time_updates:
                        delay_mapping[trip_id] = trip_delay(stop_time_updates)
        return Output(value=delay_mapping, metadata={"delay_count": len(delay_mapping)})
    raise Exception(f"API Error: {response.status_code} {response.text[:200]}")


@asset
def assign_bus_to_district(fetch_bus_vehicle_data):
    """Assign each bus to a district with a spatial join (bus point inside district polygon).
    District boundaries come straight from the GeoJSON file."""
    logger = get_dagster_logger()
    districts_gdf = load_districts_from_file()

    bus_positions = []
    for entity in fetch_bus_vehicle_data:
        if "vehicle" in entity and "position" in entity["vehicle"]:
            position = entity["vehicle"]["position"]
            bus_positions.append({
                "vehicle_id": real_vehicle_id(entity), 
                "geometry": Point(position["longitude"], position["latitude"]),
                "latitude": position["latitude"],
                "longitude": position["longitude"],
            })

    if not bus_positions:
        logger.info("No bus positions in this feed.")
        return Output(value=[], metadata={"bus_count": 0})

    bus_gdf = gpd.GeoDataFrame(bus_positions, crs="EPSG:4326")
    bus_in_districts = gpd.sjoin(bus_gdf, districts_gdf, how="left", predicate="intersects")

    bus_data = [
        {
            "vehicle_id": row["vehicle_id"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "district": row.get("Descriptor", "Unknown"),
            "town": row.get("local_authority", "Unknown"),
        }
        for _, row in bus_in_districts.iterrows()
    ]
    logger.info(f"Assigned {len(bus_data)} buses to districts.")
    return Output(value=bus_data, metadata={"bus_count": len(bus_data)})


@asset
def process_and_store_vehicle_data(fetch_bus_vehicle_data, fetch_bus_trip_delays, assign_bus_to_district):
    """Insert Dublin buses into PostgreSQL in one batch, then remove old rows
    (older than RETENTION_DAYS, or beyond the newest MAX_ROWS)."""
    logger = get_dagster_logger()
    create_table()

    vehicle_data_map = {real_vehicle_id(entity): entity for entity in fetch_bus_vehicle_data}
    delay_mapping = fetch_bus_trip_delays
    district_data_map = {data["vehicle_id"]: data for data in assign_bus_to_district}

    run_time = datetime.datetime.now()   # one timestamp for the whole snapshot

    # Build all rows first, then send them in ONE batch
    rows = []
    for vehicle_id, entity in vehicle_data_map.items():
        if "vehicle" not in entity or "position" not in entity["vehicle"]:
            continue
        position = entity["vehicle"]["position"]
        trip = entity["vehicle"].get("trip", {})
        trip_id = trip.get("tripId", "Unknown")
        route_id = trip.get("routeId")  
        delay = delay_mapping.get(trip_id, "Unknown")
        location = district_data_map.get(vehicle_id, {})
        district = str(location.get("district", "Unknown"))
        town = str(location.get("town", "Unknown"))
        # only store buses inside Dublin
        if "DUBLIN" in district or "Dublin" in town:
            rows.append((vehicle_id, trip_id, route_id, district, town,
                         position["latitude"], position["longitude"], delay, run_time))

    inserted = deleted = 0
    conn = psycopg2.connect(**db_params)
    try:
        with conn.cursor() as cur:
            if rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO dublin_bus_data (
                        vehicle_id, trip_id, route_id, district, town, latitude, longitude, delay, timestamp
                    ) VALUES %s
                    ON CONFLICT (vehicle_id, timestamp) DO NOTHING;
                    """,
                    rows,
                    page_size=max(1000, len(rows)),   # one batch, so rowcount covers every row
                )
                inserted = cur.rowcount

            # Keep data by AGE when RETENTION_DAYS is set (uses the
            # timestamp index), otherwise keep the newest MAX_ROWS rows.
            if RETENTION_DAYS > 0:
                cur.execute(
                    "DELETE FROM dublin_bus_data WHERE timestamp < now() - make_interval(days => %s);",
                    (RETENTION_DAYS,),
                )
                deleted = cur.rowcount
            elif MAX_ROWS > 0:
                # ctid is PostgreSQL's internal row address, a quick way to target rows.
                cur.execute(
                    """
                    DELETE FROM dublin_bus_data
                    WHERE ctid IN (
                        SELECT ctid FROM dublin_bus_data
                        ORDER BY timestamp DESC
                        OFFSET %s
                    );
                    """,
                    (MAX_ROWS,),
                )
                deleted = cur.rowcount

        conn.commit()
        rule = (f"last {RETENTION_DAYS} days" if RETENTION_DAYS > 0
                else f"newest {MAX_ROWS} rows" if MAX_ROWS > 0 else "no limit")
        logger.info(f"Inserted {inserted} rows, removed {deleted} old rows (keeping {rule}).")
        return Output(value=inserted, metadata={"inserted": inserted, "deleted_old_rows": deleted, "keeping": rule})
    except Exception as e:
        conn.rollback()
        logger.error(f"Error storing data: {e}")
        raise 
    finally:
        conn.close()


def create_table():
    """Create the PostgreSQL table if it doesn't exist."""
    with psycopg2.connect(**db_params) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS dublin_bus_data (
                    vehicle_id TEXT,
                    trip_id TEXT,
                    district TEXT,
                    town TEXT,
                    latitude TEXT,
                    longitude TEXT,
                    delay TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (vehicle_id, timestamp)
                );
                """
            )
            cur.execute("ALTER TABLE dublin_bus_data ADD COLUMN IF NOT EXISTS route_id TEXT;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dublin_bus_data_timestamp ON dublin_bus_data (timestamp);")
        conn.commit()
