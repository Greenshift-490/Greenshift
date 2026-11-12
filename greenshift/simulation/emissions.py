"""GS-34: Calculate carbon emissions of a schedule using ACTUAL intensity data.

Unit check: a job drawing P kW for one hour uses P kWh = P/1000 MWh. At an intensity of
I tonnes CO2 per MWh that is P/1000 * I tonnes = P * I kilograms. So per hour:

    emissions_kg = power_kw * intensity_t_per_mwh

Rules that keep results honest:
* The metric's name travels with every number (``metric`` column), so CAISO average
  emissions and, later, MIDAS marginal emissions are never added together or confused.
* If any hour a job runs in has no actual data, that job is "unscored" (NaN), never
  treated as zero.
* Schedulers are compared only on jobs that are scored under *every* scheduler, so one
  scheduler can't look better just because its jobs landed in data gaps.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Mapping, Sequence

import pandas as pd

from greenshift.scheduling import ScheduledJob


def job_emissions(schedule: Sequence[ScheduledJob], intensity: pd.Series,
                  scheduler_name: str) -> pd.DataFrame:
    """One row per job with its energy use and emissions under ``intensity``.

    ``intensity`` is indexed by hour start (UTC) in tonnes CO2 per MWh; its ``name``
    is recorded as the metric.
    """
    if not intensity.name:
        raise ValueError("intensity series must be named, e.g. 'caiso_avg_co2_t_per_mwh'")
    lookup = intensity.dropna().to_dict()

    rows = []
    for s in schedule:
        hours = [pd.Timestamp(s.start_utc + timedelta(hours=h))
                 for h in range(s.job.duration_hours)]
        scored = all(hour in lookup for hour in hours)
        rows.append({
            "scheduler": scheduler_name,
            "metric": intensity.name,
            "job_id": s.job.job_id,
            "flexible": s.job.flexible,
            "start_utc": s.start_utc,
            "delay_hours": s.delay_hours,
            "meets_deadline": s.meets_deadline,
            "energy_kwh": s.job.power_kw * s.job.duration_hours,
            "scored": scored,
            "emissions_kg": (sum(s.job.power_kw * lookup[h] for h in hours)
                             if scored else float("nan")),
        })
    return pd.DataFrame(rows)


def compare(schedules: Mapping[str, Sequence[ScheduledJob]], intensity: pd.Series,
            baseline: str = "fifo") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Score every schedule and summarize. Returns (per_job, summary).

    The summary only counts jobs scored under every scheduler (``jobs_compared``) and
    reports how many jobs couldn't be scored (``jobs_unscored``).
    """
    if baseline not in schedules:
        raise ValueError(f"baseline '{baseline}' is not among {list(schedules)}")
    per_job = pd.concat([job_emissions(schedule, intensity, name)
                         for name, schedule in schedules.items()], ignore_index=True)

    scored_everywhere = per_job.groupby("job_id")["scored"].all()
    common = scored_everywhere[scored_everywhere].index
    compared = per_job[per_job["job_id"].isin(common)]

    summary = compared.groupby("scheduler", sort=False).agg(
        jobs_compared=("job_id", "count"),
        energy_kwh=("energy_kwh", "sum"),
        emissions_kg=("emissions_kg", "sum"),
        avg_delay_hours=("delay_hours", "mean"),
        deadline_misses=("meets_deadline", lambda met: int((~met).sum())),
    )
    summary["jobs_unscored"] = int((~scored_everywhere).sum())
    summary["metric"] = intensity.name
    base = summary.loc[baseline, "emissions_kg"]
    summary["reduction_vs_baseline_pct"] = (
        (1 - summary["emissions_kg"] / base) * 100 if base > 0 else float("nan"))
    return per_job, summary.reset_index()
