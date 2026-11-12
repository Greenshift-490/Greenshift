"""CAISO Today's Outlook history: ACTUAL CO2 emissions and demand (GS-44).

This is *evaluation* data. It describes what actually happened on the grid, so it is used
to score schedules after the fact and must never be passed to a scheduler.

Source files (5-minute rows, Pacific local time, no date in the row):
    https://www.caiso.com/outlook/history/YYYYMMDD/co2.csv
        Time, Biogas CO2, Biomass CO2, Natural Gas CO2, Coal CO2, Imports CO2, ...
        values in metric tonnes of CO2 per hour (a rate, not an amount)
    https://www.caiso.com/outlook/history/YYYYMMDD/demand.csv
        Time, Day ahead forecast, Hour ahead forecast, Current demand, ...
        values in MW; "Current demand" is the actual demand

Output: one row per UTC hour with the average CO2 intensity in tonnes per MWh:

    caiso_avg_co2_t_per_mwh = mean CO2 rate (t/h) / mean demand (MW)

This is an AVERAGE emissions rate (all generation, including imports). It is kept
separate from MIDAS's *marginal* emissions. CAISO itself describes its CO2 figures as an
approximation.

Quirks found in real files (see tests):
* demand.csv ends with an extra "00:00" row that belongs to the next day. We drop it.
* On the November fall-back day, the 1 am hour happens twice, but CAISO's file lists
  01:00-01:55 only once, so we can't tell which 1 am the rows describe. By default those
  rows are dropped and both 1 am hours show up as gaps instead of guessed data.
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from greenshift.data.oasis import PACIFIC

HISTORY_URL = "https://www.caiso.com/outlook/history/{day:%Y%m%d}/{kind}.csv"
KINDS = ("co2", "demand")
SAMPLES_PER_HOUR = 12  # 5-minute data


# --- Download ---------------------------------------------------------------------

def download_outlook_csv(kind: str, day: date, out_dir: Path | str = "data/raw",
                         session: requests.Session | None = None,
                         timeout: float = 30) -> Path:
    """Download one day's file and save it with the retrieval time in its name."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    session = session or requests.Session()
    response = session.get(HISTORY_URL.format(day=day, kind=kind), timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} for {kind} on {day}")
    # CAISO sometimes answers 200 with an HTML error page, so check it's really our CSV.
    if not response.text.startswith("Time,"):
        raise RuntimeError(f"Unexpected content for {kind} on {day}: {response.text[:60]!r}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    retrieved = datetime.now(timezone.utc)
    path = out_dir / f"outlook_{kind}_{day:%Y%m%d}_retrieved_{retrieved:%Y%m%dT%H%M%SZ}.csv"
    path.write_text(response.text, encoding="utf-8")
    return path


# --- Parsing ----------------------------------------------------------------------

def parse_outlook_csv(path: Path | str, day: date, ambiguous: str = "NaT") -> pd.DataFrame:
    """Read one day's file and attach UTC timestamps.

    ``ambiguous`` controls the repeated 1 am hour on the fall-back day: "NaT" drops those
    rows (default); True treats them as daylight time; False as standard time.
    """
    df = pd.read_csv(path)
    if "Time" not in df.columns:
        raise ValueError(f"{path}: missing Time column")
    df["Time"] = df["Time"].astype(str).str.strip()

    # Trailing "00:00" row = midnight of the next day; that day's own file has it.
    if len(df) > 1 and df["Time"].iloc[-1] in ("00:00", "0:00"):
        df = df.iloc[:-1]
    if df["Time"].duplicated().any():
        raise ValueError(f"{path}: repeated times {df['Time'][df['Time'].duplicated()].tolist()[:3]}")

    local = pd.to_datetime(f"{day.isoformat()} " + df["Time"], format="%Y-%m-%d %H:%M")
    aware = local.dt.tz_localize(PACIFIC, ambiguous=ambiguous, nonexistent="NaT")
    df = df.assign(timestamp_utc=aware.dt.tz_convert("UTC")).dropna(subset=["timestamp_utc"])

    for column in df.columns.drop(["Time", "timestamp_utc"]):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.reset_index(drop=True)


def hourly_actual_emissions(co2: pd.DataFrame, demand: pd.DataFrame,
                            min_samples: int = SAMPLES_PER_HOUR) -> pd.DataFrame:
    """Combine parsed co2 and demand files into hourly average intensity.

    An hour gets an intensity only if both files have at least ``min_samples`` of its
    5-minute readings; otherwise it is NaN and shows up as a gap.
    """
    co2_columns = [c for c in co2.columns if c.endswith("CO2")]
    if not co2_columns:
        raise ValueError("No CO2 columns found")
    co2_rate = co2[co2_columns].sum(axis=1, min_count=len(co2_columns))  # NaN if any part missing
    co2_hourly = co2_rate.groupby(co2["timestamp_utc"].dt.floor("h")).agg(["mean", "count"])
    demand_hourly = (demand["Current demand"]
                     .groupby(demand["timestamp_utc"].dt.floor("h")).agg(["mean", "count"]))

    hours = co2_hourly.index.union(demand_hourly.index)
    full_range = pd.date_range(hours.min(), hours.max(), freq="h")  # make gaps visible
    co2_hourly = co2_hourly.reindex(full_range)
    demand_hourly = demand_hourly.reindex(full_range)

    result = pd.DataFrame({
        "interval_start_utc": full_range,
        "interval_end_utc": full_range + pd.Timedelta(hours=1),
        "co2_tonnes_per_hour": co2_hourly["mean"].to_numpy(),
        "demand_mw": demand_hourly["mean"].to_numpy(),
        "co2_samples": co2_hourly["count"].fillna(0).astype(int).to_numpy(),
        "demand_samples": demand_hourly["count"].fillna(0).astype(int).to_numpy(),
    })
    complete = (result["co2_samples"] >= min_samples) & (result["demand_samples"] >= min_samples)
    result["caiso_avg_co2_t_per_mwh"] = (
        result["co2_tonnes_per_hour"] / result["demand_mw"]).where(complete)
    return result


# --- Command line -----------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Download CAISO actual CO2 and demand.")
    parser.add_argument("--day", required=True, type=date.fromisoformat,
                        help="first Pacific-time day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--raw", default="data/raw")
    parser.add_argument("--out", default="data/processed/caiso_actual_emissions.csv")
    args = parser.parse_args(argv)

    co2_parts, demand_parts = [], []
    session = requests.Session()
    for offset in range(args.days):
        day = args.day + timedelta(days=offset)
        for kind, parts in (("co2", co2_parts), ("demand", demand_parts)):
            path = download_outlook_csv(kind, day, args.raw, session=session)
            parts.append(parse_outlook_csv(path, day))
            time.sleep(1)  # be polite to caiso.com

    hourly = hourly_actual_emissions(pd.concat(co2_parts), pd.concat(demand_parts))
    gaps = hourly["caiso_avg_co2_t_per_mwh"].isna().sum()
    if gaps:
        print(f"Warning: {gaps} hour(s) without complete data")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    hourly.to_csv(out, index=False)
    print(f"Saved {len(hourly)} hourly rows to {out}")


if __name__ == "__main__":
    main()
