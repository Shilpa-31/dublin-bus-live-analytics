# dublin-bus-live-analytics

**Real-time Dublin Bus analytics: where every bus is, how late it runs, which buses are least reliable, and whether the weather makes a difference.**

An end-to-end data project on live public-transport data. A **Dagster** pipeline downloads every bus position and delay from the National Transport Authority's **GTFS-Realtime** API once a minute, works out which Dublin postal district each bus is in with **GeoPandas**, and stores every snapshot in **PostgreSQL on AWS RDS**, building a continuously growing, minute-by-minute history of the city's buses. A **Streamlit** dashboard turns that history into a live map and delay analysis, joined with **OpenWeather** data.

![Live map of Dublin buses and bus stops](dashboard/live%20map.png)

More screenshots of the dashboard and the Dagster pipeline are in [`dashboard/`](dashboard/).

## What the dashboard answers

| Tab | Question |
|---|---|
| **Live Map** | Where is every bus right now and how late is it? Satellite map (Mapbox) with buses, bus stops and district outlines, a multi-select district filter, headline numbers (buses in Dublin, average delay, on time, more than 5 min late) and the current weather. |
| **Delays by Hour** | How does the average delay change through the day, and how much worse are the rush hours (7-10 am, 5-8 pm)? |
| **Bus Reliability** | Which buses run late most often? On-time % and average delay for every bus (Bus 39A, Bus C1..), with the 10 most and 10 least reliable side by side, for the period you choose. |
| **Weather Impact** | Do rain, wind and temperature line up with longer delays? Hourly delays for the chosen districts over the last 24 hours, next to the weather at the time. |

Buses are named the way passengers say them ("Bus C1", not route IDs). On the Bus Reliability tab, **on time** means between 1 minute early and 5 minutes late, a common public transport standard.

## How it works

```mermaid
flowchart LR
    NTA["NTA GTFS-Realtime API<br/>Vehicles + TripUpdates (protobuf)"] --> P
    subgraph P["Dagster pipeline · every minute"]
        direction TB
        A[fetch bus positions] --> C[assign district<br/>GeoPandas spatial join]
        B[fetch trip delays] --> D
        C --> D[batch insert]
    end
    G["Dublin postal districts<br/>35 areas, GeoJSON"] --> C
    D --> PG[("AWS RDS PostgreSQL<br/>dublin_bus_data")]
    S["Bus stops GeoJSON"] --> L[Dublin_bus_stops.py] --> PS[("PostgreSQL<br/>bus_stops_with_districts")]
    PG --> APP[Streamlit dashboard]
    PS --> APP
    G --> APP
    OW[OpenWeather API] --> APP
```

Every minute the pipeline:
1. **Fetches bus positions** (900-1,500 vehicles across Ireland) and **trip delays** (~2,000 trips) from the NTA API.
2. **Assigns each bus to a district** by checking which of the 35 Dublin postal areas its position falls in, and keeps the ~500 buses inside Dublin.
3. **Stores the snapshot** in PostgreSQL on AWS RDS in one batch.

That is about 500 new rows a minute, 30,000 an hour and over 700,000 a day, collected continuously for as long as the pipeline runs. How much history to keep is a setting (`RETENTION_DAYS`), so storage can be sized to the use case.

### Data model

`dublin_bus_data` (AWS RDS), one row per bus per minute:

| Column | Example | Meaning |
|---|---|---|
| `vehicle_id` | `33012` | the bus's own ID from the feed |
| `trip_id` | `6026_19755` | the journey the bus is running |
| `route_id` | `3_S4_a` | NTA route ID; the 2nd part (`S4`) is the bus number passengers see |
| `district` / `town` | `DUBLIN 12` / `Dublin City` | postal district and local authority |
| `latitude` / `longitude` | `53.3245` / `-6.3415` | position |
| `delay` | `757` | predicted delay in seconds (negative = early, `Unknown` = no prediction) |
| `timestamp` | `2026-11-15 17:36:23` | when the snapshot was taken |

`bus_stops_with_districts` (~5,900 stops) holds every stop with its name, position and district.

## Data quality decisions

- **Correct bus identity.** The feed's `entity.id` (e.g. `V30`) is only a row label within one download and points to different buses each time, so buses are identified by `vehicle.vehicle.id`.
- **Bus numbers from route IDs.** The NTA writes route IDs as `1_39A_c_a` (older data: `1 39A c a`); the bus number is the second part, in either format.
- **Delays for buses still at their first stop.** The feed often gives only a *departure* delay before a trip starts, so the pipeline uses the latest arrival delay and falls back to the departure delay, instead of recording `Unknown`.
- **Missing delays are kept, not zeroed.** A small share of buses have no prediction at all. They stay in the data as `Unknown` (they still show where the bus is) but are left out of every average, because 0 would mean "exactly on time".
- **Feed errors are filtered.** Delays beyond ±3 hours (e.g. trips running past midnight) are ignored in the reliability analysis, and buses with fewer than 30 checks are not ranked.
- **Complete district coverage.** The boundary file covers all 35 Eircode areas in Co. Dublin, including Dublin 13 (Howth, Sutton, Baldoyle) and Malahide; an older version left those areas, and every bus in them, out.
- **Automated data quality checks.** Six Dagster asset checks run after every pipeline step and flag problems in the Dagster UI without stopping collection: the feed returned buses, positions are under 5 minutes old, at least 80% of trips have a delay, enough buses are in Dublin for the time of day, no bus is matched to two districts, and new rows were saved.
- **Rate limits respected.** When the NTA answers "Rate limit is exceeded, try again in N seconds", the pipeline waits and retries instead of failing; a run is skipped if the previous one is still going.

## Tech stack

Python · Dagster (assets, asset checks, cron schedule) · GTFS-Realtime / protobuf · GeoPandas & Shapely (Irish Grid EPSG:2157 → GPS EPSG:4326) · PostgreSQL · AWS RDS · SQLAlchemy / psycopg2 · Streamlit · pydeck (deck.gl + Mapbox) · Altair · OpenWeather APIs · pytest

## Project structure

```text
├── dublin_bus_tracker_pipeline/          Dagster pipeline
│   ├── dublin_bus_tracker_pipeline/
│   │   ├── assets.py                     the four pipeline steps
│   │   ├── checks.py                     data quality checks (Dagster asset checks)
│   │   └── definitions.py                every-minute schedule
│   ├── dublin_bus_tracker_pipeline_tests/
│   └── pyproject.toml
├── dublin_bus_tracker_app/               Streamlit dashboard
│   ├── app.py                            page layout, Live Map, headline numbers
│   ├── delay_analysis.py                 Delays by Hour tab
│   ├── route_reliability.py              Bus Reliability tab
│   ├── weather_impact.py                 Weather Impact tab and weather API calls
│   └── .streamlit/config.toml            colour theme
├── Real_time_geo_locator/                reference data
│   ├── Dublin_bus_stops.py               loads the bus stops into PostgreSQL (run once)
│   ├── stops.geojson                     bus stop locations
│   └── dublin_postcode_boundaries.geojson  35 Dublin postal district boundaries
├── dashboard/                            live map image, dashboard pages, Dagster job and schedule
├── postgreSQL_query.sql                  handy checks: row counts, rows per minute, latest rows
└── requirements.txt
```

## Run it

**You need:** Python 3.10–3.12, PostgreSQL (local, for the bus stops), a PostgreSQL database for the bus data (AWS RDS, or the same local one), and free API keys from the [NTA developer portal](https://developer.nationaltransport.ie) and [OpenWeather](https://openweathermap.org/api). A [Mapbox](https://account.mapbox.com) token is optional, for the satellite map.

```bash
git clone https://github.com/YOUR_USERNAME/dublin-bus-live-analytics.git
cd dublin-bus-live-analytics
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

1. **Create the three `.env` files** described under [Settings](#settings).
2. **Load the bus stops** (once):
   ```bash
   python Real_time_geo_locator/Dublin_bus_stops.py
   ```
3. **Start the pipeline** in one terminal. It collects data every minute; the Dagster UI is at http://localhost:3000.
   ```bash
   cd dublin_bus_tracker_pipeline
   caffeinate -dims dagster dev/dagster dev        # macOS: caffeinate keeps the Mac awake
   ```
4. **Start the dashboard** in a second terminal (with `.venv` activated); it opens at http://localhost:8501.
   ```bash
   cd dublin_bus_tracker_app
   streamlit run app.py
   ```

Run the tests with `pytest` from `dublin_bus_tracker_pipeline/`.

### Storing the bus data on AWS RDS

1. Create a **PostgreSQL** RDS instance (Full configuration, Free tier template, db.t4g.micro, 20 GiB, **Public access: Yes**, initial database name `dublin_bus`).
2. In its security group, allow **PostgreSQL (port 5432) from your IP**.
3. Point the pipeline's `DB_*` settings at the RDS endpoint with `DB_SSLMODE=require`, and set the dashboard's `BUS_DB_URL` to the same database with `?sslmode=require`.


## Settings

Each folder needs its own `.env` file. They hold keys and passwords, so they are never committed (see `.gitignore`).

**`dublin_bus_tracker_pipeline/dublin_bus_tracker_pipeline/.env`**

| Setting | Example | Purpose |
|---|---|---|
| `VEHICLES_API_URL` | `https://api.nationaltransport.ie/gtfsr/v2/Vehicles` | NTA bus positions feed |
| `TRIP_UPDATES_API_URL` | `https://api.nationaltransport.ie/gtfsr/v2/TripUpdates` | NTA trip delays feed |
| `API_KEY` | | your NTA API key (primary key) |
| `CONTENT_TYPE` | `application/x-protobuf` | format of the NTA feed |
| `DB_HOST`, `DB_PORT` | `xxxx.eu-west-1.rds.amazonaws.com`, `5432` | the PostgreSQL server for the bus data |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `dublin_bus`, `postgres`, … | database and login |
| `DB_SSLMODE` | `require` | `require` for AWS RDS, `prefer` for a local database |
| `RETENTION_DAYS` | `0` | how many days of history to keep (0 = keep everything) |
| `MAX_ROWS` | `0` | alternative limit: keep only the newest N rows (used when `RETENTION_DAYS` is 0) |

**`dublin_bus_tracker_app/.env`**

| Setting | Example | Purpose |
|---|---|---|
| `BUS_DB_URL` | `postgresql://postgres:PASSWORD@xxxx.rds.amazonaws.com:5432/dublin_bus?sslmode=require` | the bus data (same database the pipeline writes to) |
| `DB_URL` | `postgresql://postgres:PASSWORD@localhost:5432/Smart_City` | the bus stops |
| `WEATHER_API_KEY` | | your OpenWeather key |
| `REFRESH_SECONDS` | `60` | how often the dashboard refreshes |
| `MAPBOX_API_KEY` | `pk.…` | optional: satellite map (without it, a free street map is used) |
| `MAP_STYLE` | `mapbox://styles/mapbox/satellite-streets-v11` | optional: Mapbox map style |

**`Real_time_geo_locator/.env`**

| Setting | Purpose |
|---|---|
| `DB_URL` | where to load the bus stops; the same database as the dashboard's `DB_URL` |

## Future enhancements

Planned next steps to take the project further:

- **Run the pipeline fully in the cloud** (AWS ECS/EC2 or Dagster+), so data collection never depends on a laptop staying awake.
- **Compare against the published timetable** (NTA GTFS static data) to measure stop-by-stop punctuality, headways and bus bunching, not only end-of-trip delays.
- **Store the weather history** in the database every hour, so weather and delays can be compared over months instead of the last 24 hours.
- **Delay prediction:** train a model on the collected history (time of day, day of week, route, district, weather) to forecast how late a bus will be.
- **Daily summary tables** (e.g. with dbt) for on-time % per bus, district and hour, so long-term trends load instantly in the dashboard.
- **Alerts** (email or Slack) when a data quality check fails, the feed stops, or a route's delays spike.
- **Events and disruption analysis:** measure the effect of match days, concerts, roadworks and school terms on delays.
- **Wider coverage:** add Luas, DART and Bus Éireann services from the same NTA feed.
- **Deployment:** containerise with Docker, add CI tests with GitHub Actions, and publish the dashboard online.

## Data sources and credits

- **Bus positions and delays:** [National Transport Authority, GTFS-Realtime API](https://developer.nationaltransport.ie)
- **Weather:** [OpenWeather](https://openweathermap.org/api) (current weather and One Call 4.0 hourly timeline)
- **Dublin postal district boundaries:** [autoaddress.ie / Rowan Molony, *Dublin Postcode Boundaries* v10, Zenodo](https://zenodo.org/records/4589220), CC BY 4.0
- **Bus stop locations:** `Real_time_geo_locator/stops.geojson`
- **Maps:** © Mapbox, © OpenStreetMap contributors, © Maxar (satellite imagery), © CARTO
