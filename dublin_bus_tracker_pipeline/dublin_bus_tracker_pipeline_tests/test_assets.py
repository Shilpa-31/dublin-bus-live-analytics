"""Unit tests for the pipeline's pure helpers (no network or database needed).

Run from dublin_bus_tracker_pipeline/:  pytest
"""
from dublin_bus_tracker_pipeline.assets import real_vehicle_id
from dublin_bus_tracker_pipeline.definitions import defs, every_minute_schedule


def test_vehicle_id_comes_from_vehicle_descriptor():
    # entity["id"] ("V30") is only a row label inside one feed download
    entity = {"id": "V30", "vehicle": {"vehicle": {"id": "123456"}}}
    assert real_vehicle_id(entity) == "123456"


def test_vehicle_id_falls_back_to_label_then_row_id():
    assert real_vehicle_id({"id": "V1", "vehicle": {"vehicle": {"label": "SG123"}}}) == "SG123"
    assert real_vehicle_id({"id": "V1", "vehicle": {}}) == "V1"


def test_definitions_load_with_every_minute_schedule():
    assert every_minute_schedule.cron_schedule == "* * * * *"
    assert defs.get_job_def("my_asset_job") is not None


def test_delay_check_warns_when_too_many_trips_have_no_delay():
    from dublin_bus_tracker_pipeline.checks import most_trips_have_delay
    assert most_trips_have_delay({"t1": 60, "t2": 0, "t3": -30, "t4": 90, "t5": 120}).passed
    assert not most_trips_have_delay({"t1": 60, "t2": "Unknown"}).passed


def test_duplicate_check_finds_a_bus_in_two_districts():
    from dublin_bus_tracker_pipeline.checks import one_district_per_bus
    ok = [{"vehicle_id": "1", "district": "DUBLIN 1"}, {"vehicle_id": "2", "district": "DUBLIN 2"}]
    dup = ok + [{"vehicle_id": "1", "district": "DUBLIN 7"}]
    assert one_district_per_bus(ok).passed
    assert not one_district_per_bus(dup).passed
