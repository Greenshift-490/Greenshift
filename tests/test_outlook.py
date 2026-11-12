"""Tests for CAISO Today's Outlook actual emissions (GS-44).

The sample files copy the layout of real downloads from caiso.com/outlook/history
(checked on 2026-10-01 and 2026-11-01), including two quirks: the extra trailing "00:00"
row in demand.csv, and the fall-back day listing 1 am only once.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from greenshift.data.outlook import (
    download_outlook_csv,
    hourly_actual_emissions,
    parse_outlook_csv,
)

CO2_HEADER = "Time,Biogas CO2,Biomass CO2,Natural Gas CO2,Coal CO2,Imports CO2,Geothermal CO2"
DEMAND_HEADER = "Time,Day ahead forecast,Hour ahead forecast,Current demand,Demand response"


def times(hours=24):
    start = pd.Timestamp("2000-01-01")
    return [(start + timedelta(minutes=5 * i)).strftime("%H:%M") for i in range(hours * 12)]


def co2_file(tmp_path, gas=6000, imports=1800, name="co2.csv"):
    rows = [f"{t},80,80,{gas},2,{imports},8" for t in times()]  # total = gas + imports + 170
    path = tmp_path / name
    path.write_text("\n".join([CO2_HEADER, *rows]) + "\n")
    return path


def demand_file(tmp_path, mw=25000, trailing_midnight=True, name="demand.csv"):
    rows = [f"{t},0,0,{mw}," for t in times()]
    if trailing_midnight:
        rows.append(f"00:00,0,0,{mw},")
    path = tmp_path / name
    path.write_text("\n".join([DEMAND_HEADER, *rows]) + "\n")
    return path


OCT_1 = date(2026, 10, 1)   # normal 24-hour day
NOV_1 = date(2026, 11, 1)   # fall-back day: 1 am happens twice


# --- Parsing ----------------------------------------------------------------------

def test_parse_converts_pacific_times_to_utc(tmp_path):
    df = parse_outlook_csv(co2_file(tmp_path), OCT_1)
    assert len(df) == 288
    assert df["timestamp_utc"].iloc[0] == pd.Timestamp("2026-10-01T07:00Z")  # midnight PDT


def test_trailing_next_day_midnight_is_dropped(tmp_path):
    df = parse_outlook_csv(demand_file(tmp_path), OCT_1)
    assert len(df) == 288
    assert df["timestamp_utc"].max() == pd.Timestamp("2026-10-02T06:55Z")


def test_ambiguous_fall_back_hour_is_dropped_by_default(tmp_path):
    df = parse_outlook_csv(co2_file(tmp_path), NOV_1)
    assert len(df) == 288 - 12  # 01:00-01:55 can't be placed, so it's left out


def test_ambiguous_hour_can_be_assumed_daylight_time(tmp_path):
    df = parse_outlook_csv(co2_file(tmp_path), NOV_1, ambiguous=True)
    assert len(df) == 288
    one_am = df[df["Time"] == "01:00"]["timestamp_utc"].iloc[0]
    assert one_am == pd.Timestamp("2026-11-01T08:00Z")  # 1 am PDT


def test_repeated_times_are_an_error(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(f"{CO2_HEADER}\n00:00,1,1,1,1,1,1\n00:00,1,1,1,1,1,1\n00:05,1,1,1,1,1,1\n")
    with pytest.raises(ValueError, match="repeated"):
        parse_outlook_csv(path, OCT_1)


def test_missing_time_column_is_an_error(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("Hour,Value\n1,2\n")
    with pytest.raises(ValueError, match="Time"):
        parse_outlook_csv(path, OCT_1)


# --- Hourly intensity -------------------------------------------------------------

def test_hourly_intensity_is_co2_rate_over_demand(tmp_path):
    co2 = parse_outlook_csv(co2_file(tmp_path, gas=6000, imports=1830), OCT_1)
    demand = parse_outlook_csv(demand_file(tmp_path, mw=25000), OCT_1)
    hourly = hourly_actual_emissions(co2, demand)
    assert len(hourly) == 24
    # total CO2 = 80 + 80 + 6000 + 2 + 1830 + 8 = 8000 t/h; 8000 / 25000 = 0.32 t/MWh
    assert hourly["caiso_avg_co2_t_per_mwh"].iloc[0] == pytest.approx(0.32)


def test_fall_back_day_shows_both_1am_hours_as_gaps(tmp_path):
    co2 = parse_outlook_csv(co2_file(tmp_path), NOV_1)
    demand = parse_outlook_csv(demand_file(tmp_path), NOV_1)
    hourly = hourly_actual_emissions(co2, demand)
    assert len(hourly) == 25
    gaps = hourly[hourly["caiso_avg_co2_t_per_mwh"].isna()]["interval_start_utc"]
    assert list(gaps) == [pd.Timestamp("2026-11-01T08:00Z"), pd.Timestamp("2026-11-01T09:00Z")]


def test_incomplete_hour_has_no_intensity(tmp_path):
    co2 = parse_outlook_csv(co2_file(tmp_path), OCT_1).drop(index=[0])  # one reading missing
    demand = parse_outlook_csv(demand_file(tmp_path), OCT_1)
    hourly = hourly_actual_emissions(co2, demand)
    assert pd.isna(hourly["caiso_avg_co2_t_per_mwh"].iloc[0])
    assert hourly["co2_samples"].iloc[0] == 11
    assert hourly["caiso_avg_co2_t_per_mwh"].iloc[1:].notna().all()


def test_missing_co2_component_makes_that_reading_unknown(tmp_path):
    co2 = parse_outlook_csv(co2_file(tmp_path), OCT_1)
    co2.loc[0, "Imports CO2"] = float("nan")
    hourly = hourly_actual_emissions(co2, parse_outlook_csv(demand_file(tmp_path), OCT_1))
    assert hourly["co2_samples"].iloc[0] == 11  # not counted as a lower total


# --- Download (fake session, no network) ------------------------------------------

class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code, self.text = status_code, text


class FakeSession:
    def __init__(self, response):
        self.response, self.urls = response, []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return self.response


def test_download_saves_file_with_retrieval_time(tmp_path):
    session = FakeSession(FakeResponse(200, f"{CO2_HEADER}\n00:00,1,1,1,1,1,1\n"))
    path = download_outlook_csv("co2", OCT_1, tmp_path, session=session)
    assert session.urls == ["https://www.caiso.com/outlook/history/20261001/co2.csv"]
    assert path.name.startswith("outlook_co2_20261001_retrieved_")
    assert path.read_text().startswith("Time,")


def test_download_rejects_html_error_page(tmp_path):
    session = FakeSession(FakeResponse(200, "<html>Service unavailable</html>"))
    with pytest.raises(RuntimeError, match="Unexpected content"):
        download_outlook_csv("co2", OCT_1, tmp_path, session=session)


def test_download_rejects_http_error(tmp_path):
    with pytest.raises(RuntimeError, match="HTTP 404"):
        download_outlook_csv("demand", OCT_1, tmp_path, session=FakeSession(FakeResponse(404, "")))
