"""End-to-end simulation run: jobs -> FIFO and carbon-aware -> actual emissions.

    python -m greenshift.simulation.run --day 2026-10-01 \
        --forecast data/processed/caiso_hourly.csv \
        --actual data/processed/caiso_actual_emissions.csv

Each run gets its own folder under data/processed/runs/ holding everything needed to
reproduce or audit it: the run settings, the jobs, the exact forecast the schedulers saw,
the per-job results and the summary.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from greenshift.carbon.approximation import renewable_share_signal
from greenshift.data.oasis import pacific_day_range
from greenshift.scheduling import CarbonAwareScheduler, FifoScheduler
from greenshift.simulation.emissions import compare
from greenshift.simulation.runner import run_schedulers
from greenshift.workload.jobs import generate_jobs, jobs_to_frame

ACTUAL_METRIC = "caiso_avg_co2_t_per_mwh"


def _read_hourly(path: Path | str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["interval_start_utc"] = pd.to_datetime(df["interval_start_utc"], utc=True)
    return df


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Compare FIFO and carbon-aware scheduling.")
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--forecast", default="data/processed/caiso_hourly.csv",
                        help="GS-24 hourly table (OASIS forecasts)")
    parser.add_argument("--actual", default="data/processed/caiso_actual_emissions.csv",
                        help="GS-44 hourly actual emissions")
    parser.add_argument("--jobs", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--flexible-share", type=float, default=0.6)
    parser.add_argument("--out-dir", default="data/processed/runs")
    args = parser.parse_args(argv)

    start, end = pacific_day_range(args.day, args.days)
    jobs = generate_jobs(args.jobs, start, end, args.seed, args.flexible_share)

    forecast = renewable_share_signal(_read_hourly(args.forecast))
    forecast = forecast[(forecast.index >= start) & (forecast.index < end)]
    if forecast.empty:
        raise SystemExit(f"Forecast file has no hours between {start} and {end}")

    actual_df = _read_hourly(args.actual)
    actual = actual_df.set_index("interval_start_utc")[ACTUAL_METRIC].rename(ACTUAL_METRIC)

    schedules = run_schedulers(jobs, [FifoScheduler(), CarbonAwareScheduler()], forecast)
    per_job, summary = compare(schedules, actual, baseline="fifo")

    run_dir = Path(args.out_dir) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "settings.json").write_text(json.dumps(
        {**vars(args), "day": args.day.isoformat(),
         "forecast_signal": forecast.name, "actual_metric": ACTUAL_METRIC}, indent=2))
    jobs_to_frame(jobs).to_csv(run_dir / "jobs.csv", index=False)
    forecast.rename_axis("interval_start_utc").to_csv(run_dir / "forecast_used.csv")
    per_job.to_csv(run_dir / "per_job.csv", index=False)
    summary.to_csv(run_dir / "summary.csv", index=False)

    columns = ["scheduler", "jobs_compared", "emissions_kg", "avg_delay_hours",
               "deadline_misses", "reduction_vs_baseline_pct"]
    print(summary[columns].round(2).to_string(index=False))
    print(f"\nMetric: {ACTUAL_METRIC} (CAISO average, actual). "
          f"Schedulers saw only: {forecast.name}.")
    unscored = int(summary["jobs_unscored"].iloc[0])
    if unscored:
        print(f"Warning: {unscored} job(s) ran during hours without actual data and were "
              "left out of the comparison.")
    print(f"Saved run to {run_dir}")


if __name__ == "__main__":
    main()
