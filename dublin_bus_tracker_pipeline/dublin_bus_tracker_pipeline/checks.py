"""
Data quality checks, run automatically after each pipeline step every minute.

They never stop the pipeline: a failed check shows as a warning in the Dagster UI
(Assets -> Checks), so bad data is visible even when the run itself succeeds.
"""
import datetime
import statistics
from zoneinfo import ZoneInfo

from dagster import AssetCheckResult, AssetCheckSeverity, asset_check

DUBLIN_TZ = ZoneInfo("Europe/Dublin")

MAX_POSITION_AGE_S = 300      # median bus position should be under 5 minutes old
MIN_DELAY_SHARE = 0.80        # at least 80% of trips should have a delay prediction
MIN_BUSES_DAYTIME = 200       # 6:00-23:00 Dublin time; far fewer buses run overnight


def is_daytime(now=None):
    hour = (now or datetime.datetime.now(DUBLIN_TZ)).hour
    return 6 <= hour < 23


def warn(passed, description, **metadata):
    return AssetCheckResult(passed=passed, severity=AssetCheckSeverity.WARN,
                            description=description, metadata=metadata)


@asset_check(asset="fetch_bus_vehicle_data", description="The NTA feed returned bus positions.")
def feed_has_buses(fetch_bus_vehicle_data):
    n = len(fetch_bus_vehicle_data)
    return warn(n > 0, f"{n} vehicles in the feed", vehicles=n)


@asset_check(asset="fetch_bus_vehicle_data",
             description="Bus positions are recent (median under 5 minutes old).")
def positions_are_fresh(fetch_bus_vehicle_data):
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    ages = [now - int(e["vehicle"]["timestamp"])
            for e in fetch_bus_vehicle_data if e.get("vehicle", {}).get("timestamp")]
    if not ages:
        return warn(False, "No position timestamps in the feed")
    median_age = round(statistics.median(ages))
    return warn(median_age <= MAX_POSITION_AGE_S,
                f"Median position age {median_age} s", median_age_seconds=median_age)


@asset_check(asset="fetch_bus_trip_delays",
             description="Most trips have a delay prediction (not 'Unknown').")
def most_trips_have_delay(fetch_bus_trip_delays):
    total = len(fetch_bus_trip_delays)
    known = sum(1 for d in fetch_bus_trip_delays.values() if isinstance(d, (int, float)))
    share = known / total if total else 0.0
    return warn(share >= MIN_DELAY_SHARE, f"{share:.0%} of {total} trips have a delay",
                trips=total, with_delay=known, share_with_delay=round(share, 3))


@asset_check(asset="assign_bus_to_district",
             description="Enough buses are inside Dublin for the time of day.")
def enough_buses_in_dublin(assign_bus_to_district):
    in_dublin = {b["vehicle_id"] for b in assign_bus_to_district
                 if isinstance(b.get("district"), str)}
    minimum = MIN_BUSES_DAYTIME if is_daytime() else 1
    return warn(len(in_dublin) >= minimum,
                f"{len(in_dublin)} buses in Dublin (expected at least {minimum})",
                buses_in_dublin=len(in_dublin), expected_minimum=minimum)


@asset_check(asset="assign_bus_to_district",
             description="Each bus is matched to at most one district.")
def one_district_per_bus(assign_bus_to_district):
    seen, duplicates = set(), set()
    for b in assign_bus_to_district:
        (duplicates if b["vehicle_id"] in seen else seen).add(b["vehicle_id"])
    return warn(not duplicates, f"{len(duplicates)} buses matched to more than one district",
                duplicate_buses=len(duplicates))


@asset_check(asset="process_and_store_vehicle_data",
             description="New rows were saved to the database.")
def rows_were_saved(process_and_store_vehicle_data):
    n = process_and_store_vehicle_data or 0
    return warn(n > 0, f"{n} rows saved", rows_saved=n)
