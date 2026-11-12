"""Clean raw OASIS downloads and combine them into one hourly table (GS-24).

Input: the CSVs written by ``greenshift.data.oasis`` (SLD_FCST and SLD_REN_FCST).
Output: one row per hour for the CAISO balancing area:

    interval_start_utc | interval_end_utc | demand_mw | solar_mw | wind_mw
    | demand_retrieved_at_utc | renewables_retrieved_at_utc

What "clean" means here:
* SLD_FCST covers ~36 areas, including Western Energy Imbalance Market members outside
  California. We keep only ``CA ISO-TAC``, the CAISO total. Sub-areas such as PGE-TAC are
  already inside that total, so adding them would double-count.
* SLD_REN_FCST reports solar and wind per trading hub. CAISO's hubs are NP15, SP15 and
  ZP26; anything else (e.g. ``AVA_``) is outside CAISO and is dropped.
* Duplicate intervals are an error, not something to silently average away.
* Missing hours stay as empty values (NaN) and are reported by ``missing_hours`` rather
  than filled in, so nobody mistakes invented numbers for CAISO data.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

CAISO_TAC_AREA = "CA ISO-TAC"
CAISO_TRADING_HUBS = ("NP15", "SP15", "ZP26")
TIME_COLUMNS = ["interval_start_utc", "interval_end_utc", "retrieved_at_utc"]


def load_raw(path: Path | str) -> pd.DataFrame:
    """Read a CSV saved by ``oasis.save_raw``, restoring UTC timestamps."""
    df = pd.read_csv(path)
    for column in TIME_COLUMNS:
        df[column] = pd.to_datetime(df[column], utc=True)
    return df


def clean_demand(raw: pd.DataFrame) -> pd.DataFrame:
    """CAISO-wide demand forecast, one row per interval."""
    df = raw[raw["TAC_AREA_NAME"] == CAISO_TAC_AREA]
    if df.empty:
        raise ValueError(f"No {CAISO_TAC_AREA} rows found in demand data")
    _require_unique_intervals(df, "demand")
    return (df.rename(columns={"MW": "demand_mw",
                               "retrieved_at_utc": "demand_retrieved_at_utc"})
              [["interval_start_utc", "interval_end_utc", "demand_mw",
                "demand_retrieved_at_utc"]]
              .sort_values("interval_start_utc", ignore_index=True))


def clean_renewables(raw: pd.DataFrame) -> pd.DataFrame:
    """Solar and wind forecasts summed over CAISO's trading hubs, one row per interval."""
    df = raw[raw["TRADING_HUB"].isin(CAISO_TRADING_HUBS)]
    if df.empty:
        raise ValueError("No CAISO trading hub rows found in renewables data")
    _require_unique_intervals(df, "renewables", extra_keys=["TRADING_HUB", "RENEWABLE_TYPE"])

    totals = (df.pivot_table(index=["interval_start_utc", "interval_end_utc"],
                             columns="RENEWABLE_TYPE", values="MW", aggfunc="sum")
                .rename(columns={"Solar": "solar_mw", "Wind": "wind_mw"})
                .reset_index())
    totals.columns.name = None
    for column in ("solar_mw", "wind_mw"):
        if column not in totals:
            totals[column] = float("nan")
    totals["renewables_retrieved_at_utc"] = df["retrieved_at_utc"].max()
    return totals[["interval_start_utc", "interval_end_utc", "solar_mw", "wind_mw",
                   "renewables_retrieved_at_utc"]]


def combine(demand: pd.DataFrame, renewables: pd.DataFrame) -> pd.DataFrame:
    """Join cleaned demand and renewables on the interval. Hours present in only one
    input are kept, with NaN for the missing side."""
    combined = demand.merge(renewables, on=["interval_start_utc", "interval_end_utc"],
                            how="outer", validate="one_to_one")
    return combined.sort_values("interval_start_utc", ignore_index=True)


def missing_hours(combined: pd.DataFrame) -> pd.DatetimeIndex:
    """Hours between the first and last interval that are absent or incomplete."""
    expected = pd.date_range(combined["interval_start_utc"].min(),
                             combined["interval_start_utc"].max(), freq="h")
    complete = combined.dropna(subset=["demand_mw", "solar_mw", "wind_mw"])
    return expected.difference(pd.DatetimeIndex(complete["interval_start_utc"]))


def _require_unique_intervals(df: pd.DataFrame, name: str, extra_keys=()) -> None:
    keys = ["interval_start_utc", *extra_keys]
    duplicates = df[df.duplicated(subset=keys, keep=False)]
    if not duplicates.empty:
        first = duplicates["interval_start_utc"].iloc[0]
        raise ValueError(f"Duplicate {name} rows for interval starting {first}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Combine OASIS downloads into one hourly table.")
    parser.add_argument("--demand", required=True, help="SLD_FCST CSV from data/raw")
    parser.add_argument("--renewables", required=True, help="SLD_REN_FCST CSV from data/raw")
    parser.add_argument("--out", default="data/processed/caiso_hourly.csv")
    args = parser.parse_args(argv)

    combined = combine(clean_demand(load_raw(args.demand)),
                       clean_renewables(load_raw(args.renewables)))
    gaps = missing_hours(combined)
    if len(gaps):
        print(f"Warning: {len(gaps)} incomplete hour(s), first at {gaps[0]}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out, index=False)
    print(f"Saved {len(combined)} hourly rows to {out}")


if __name__ == "__main__":
    main()
