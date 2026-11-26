import geopandas as gpd
import io
import os
from pathlib import Path
from sqlalchemy import create_engine, MetaData, Table, Column, String, Float, text
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv


HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
DATABASE_URL = os.getenv("DB_URL")

if DATABASE_URL and DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

stops_path = HERE / "stops.geojson"
districts_path = HERE / "dublin_postcode_boundaries.geojson"

# Step 1: decode the raw stops file (Irish names with fadas may not be UTF-8)
with open(stops_path, "rb") as file:
    raw_content = file.read()

for encoding in ["utf-8", "iso-8859-1", "windows-1252"]:
    try:
        decoded_content = raw_content.decode(encoding)
        print(f"File decoded successfully with {encoding}!")
        break
    except UnicodeDecodeError:
        print(f"Failed to decode with {encoding}.")

# Drop anything before the first '{' (e.g. a byte-order mark) and read it straight from memory
cleaned_geojson = decoded_content[decoded_content.find("{"):]

# Step 2: load stops and district boundaries, same coordinate system
bus_stops = gpd.read_file(io.BytesIO(cleaned_geojson.encode("utf-8")))

# The NTA stops file stores coordinates as TEXT ("-6.27" in quotes),
# which is not valid GeoJSON, so GeoPandas reads every geometry as empty and the
# spatial join matches 0 stops. Rebuild the points from the Latitude/Longitude columns.
if bus_stops.geometry.isna().all() and {"Latitude", "Longitude"} <= set(bus_stops.columns):
    bus_stops["Latitude"] = bus_stops["Latitude"].astype(float)
    bus_stops["Longitude"] = bus_stops["Longitude"].astype(float)
    bus_stops = bus_stops.set_geometry(
        gpd.points_from_xy(bus_stops["Longitude"], bus_stops["Latitude"]), crs="EPSG:4326"
    )
    print("Rebuilt stop locations from the Latitude/Longitude columns.")

districts = gpd.read_file(districts_path).to_crs(bus_stops.crs)

# Step 3: spatial join, keep only stops inside a Dublin district
bus_stops_with_districts = gpd.sjoin(bus_stops, districts, how="inner", predicate="within")

# Clean name of columns
final_dataset = bus_stops_with_districts[["PtStopId", "AtcoCode", "SCN_English", "geometry", "Descriptor"]].rename(
    columns={"PtStopId": "busstopid", "AtcoCode": "stop_plate", "SCN_English": "name", "Descriptor": "district"}
)
final_dataset["latitude"] = final_dataset.geometry.y
final_dataset["longitude"] = final_dataset.geometry.x
final_dataset = final_dataset[["busstopid", "stop_plate", "name", "latitude", "longitude", "district"]]
final_dataset = final_dataset.drop_duplicates(subset="busstopid")  # primary key must be unique
final_dataset["busstopid"] = final_dataset["busstopid"].astype(str)
print(final_dataset.head())
print(f"Number of rows in final_dataset: {len(final_dataset)}")

# Step 4: create the table (with a primary key) and load the data
engine = create_engine(DATABASE_URL)
metadata = MetaData()
bus_stops_table = Table(
    "bus_stops_with_districts", metadata,
    Column("busstopid", String, primary_key=True),
    Column("stop_plate", String),
    Column("name", String),
    Column("latitude", Float),
    Column("longitude", Float),
    Column("district", String),
)
metadata.create_all(engine)
print("Table ready in the database.")

try:
    # Empty the table, then append the stops, so the table keeps its primary key.
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM bus_stops_with_districts"))
    final_dataset.to_sql("bus_stops_with_districts", engine, if_exists="append", index=False)
    print(f"Data has been successfully uploaded to PostgreSQL ({len(final_dataset)} stops).")
except SQLAlchemyError as e:
    print(f"An error occurred: {e}")