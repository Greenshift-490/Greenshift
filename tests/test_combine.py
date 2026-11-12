"""Tests for cleaning and combining OASIS data.

The tiny frames below use the same columns as real downloads (checked against
SLD_FCST and SLD_REN_FCST for 2026-10-01), keeping only what the code reads.
"""

import pandas as pd
import pytest

from greenshift.data.combine import (
    clean_demand,
    clean_renewables,
    combine,
    load_raw,
    missing_hours,
)

RETRIEVED = pd.Timestamp("2026-11-12T20:29:22Z")


def hour(n):
    """Interval start/end for hour n of 2026-10-01 (Pacific midnight = 07:00 UTC)."""
    start = pd.Timestamp("2026-10-01T07:00Z") + pd.Timedelta(hours=n)
    return start, start + pd.Timedelta(hours=1)


def demand_rows(hours, area="CA ISO-TAC", mw=30000.0):
    return [{"interval_start_utc": hour(h)[0], "interval_end_utc": hour(h)[1],
             "TAC_AREA_NAME": area, "MW": mw + h, "retrieved_at_utc": RETRIEVED}
            for h in hours]


def renewable_rows(hours, hub, kind, mw):
    return [{"interval_start_utc": hour(h)[0], "interval_end_utc": hour(h)[1],
             "TRADING_HUB": hub, "RENEWABLE_TYPE": kind, "MW": mw,
             "retrieved_at_utc": RETRIEVED}
            for h in hours]


# --- Demand -----------------------------------------------------------------------

def test_demand_keeps_only_caiso_total():
    raw = pd.DataFrame(demand_rows([0, 1]) + demand_rows([0, 1], area="PACW", mw=1860)
                       + demand_rows([0, 1], area="PGE-TAC", mw=12000))
    df = clean_demand(raw)
    assert df["demand_mw"].tolist() == [30000.0, 30001.0]


def test_demand_duplicate_interval_is_an_error():
    raw = pd.DataFrame(demand_rows([0]) + demand_rows([0]))
    with pytest.raises(ValueError, match="Duplicate demand"):
        clean_demand(raw)


def test_demand_without_caiso_rows_is_an_error():
    with pytest.raises(ValueError, match="CA ISO-TAC"):
        clean_demand(pd.DataFrame(demand_rows([0], area="PACW")))


# --- Renewables -------------------------------------------------------------------

def test_renewables_sum_caiso_hubs_and_drop_others():
    raw = pd.DataFrame(renewable_rows([0], "NP15", "Solar", 100)
                       + renewable_rows([0], "SP15", "Solar", 200)
                       + renewable_rows([0], "ZP26", "Solar", 300)
                       + renewable_rows([0], "SP15", "Wind", 50)
                       + renewable_rows([0], "AVA_", "Solar", 999))  # outside CAISO
    df = clean_renewables(raw)
    assert df["solar_mw"].iloc[0] == 600
    assert df["wind_mw"].iloc[0] == 50


def test_renewables_duplicate_hub_row_is_an_error():
    raw = pd.DataFrame(renewable_rows([0], "NP15", "Solar", 100) * 2)
    with pytest.raises(ValueError, match="Duplicate renewables"):
        clean_renewables(raw)


# --- Combining --------------------------------------------------------------------

def test_combine_lines_up_hours_and_keeps_both_retrieval_times():
    demand = clean_demand(pd.DataFrame(demand_rows([0, 1])))
    renewables = clean_renewables(pd.DataFrame(
        renewable_rows([0, 1], "SP15", "Solar", 10) + renewable_rows([0, 1], "SP15", "Wind", 5)))
    df = combine(demand, renewables)
    assert len(df) == 2
    assert df.loc[1, ["demand_mw", "solar_mw", "wind_mw"]].tolist() == [30001.0, 10, 5]
    assert df["demand_retrieved_at_utc"].notna().all()
    assert df["renewables_retrieved_at_utc"].notna().all()


def test_missing_hour_is_reported_not_filled():
    demand = clean_demand(pd.DataFrame(demand_rows([0, 1, 2])))
    renewables = clean_renewables(pd.DataFrame(            # hour 1 missing
        renewable_rows([0, 2], "SP15", "Solar", 10) + renewable_rows([0, 2], "SP15", "Wind", 5)))
    df = combine(demand, renewables)
    assert pd.isna(df.loc[1, "solar_mw"])
    assert list(missing_hours(df)) == [hour(1)[0]]


def test_load_raw_restores_utc_timestamps(tmp_path):
    path = tmp_path / "demand.csv"
    pd.DataFrame(demand_rows([0])).to_csv(path, index=False)
    df = load_raw(path)
    assert str(df["interval_start_utc"].dt.tz) == "UTC"
    assert df["interval_start_utc"].iloc[0] == hour(0)[0]
