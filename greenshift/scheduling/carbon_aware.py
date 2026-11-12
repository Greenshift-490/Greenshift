"""GS-38: Delay flexible jobs to cleaner hours (deadlines enforced via GS-39).

For each job:
* Inflexible job: start on arrival, exactly like FIFO.
* Flexible job: look at every feasible start time (deadlines.py), add up the forecast
  carbon signal over the hours the job would run, and pick the lowest total. Ties go to
  the earliest start, so a job is never delayed unless waiting is actually cleaner.

If the forecast has gaps, start times that would run through a missing hour are skipped.
If no start time has complete forecast data, the job starts on arrival. Without
information, the safe choice is not to delay.

Power draw is constant per job, so it doesn't affect *which* hour is cleanest; it only
matters when emissions are calculated (GS-34).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

import pandas as pd

from greenshift.scheduling.base import ScheduledJob
from greenshift.scheduling.deadlines import check_deadlines, feasible_start_times
from greenshift.workload.jobs import Job


class CarbonAwareScheduler:
    name = "carbon_aware"

    def schedule(self, jobs: Sequence[Job],
                 forecast: pd.Series | None = None) -> list[ScheduledJob]:
        if forecast is None:
            raise ValueError("CarbonAwareScheduler needs a carbon forecast")
        if not isinstance(forecast.index, pd.DatetimeIndex) or forecast.index.tz is None:
            raise ValueError("forecast must be indexed by timezone-aware hourly timestamps")
        lookup = forecast.dropna().to_dict()

        ordered = sorted(jobs, key=lambda job: (job.arrival_utc, job.job_id))
        schedule = [ScheduledJob(job, self._best_start(job, lookup)) for job in ordered]
        check_deadlines(schedule)
        return schedule

    @staticmethod
    def _best_start(job: Job, lookup: dict) -> datetime:
        if not job.flexible:
            return job.arrival_utc
        best_start, best_total = job.arrival_utc, None
        for start in feasible_start_times(job):
            hours = [pd.Timestamp(start + timedelta(hours=h)) for h in range(job.duration_hours)]
            if not all(hour in lookup for hour in hours):
                continue  # forecast gap: can't judge this option
            total = sum(lookup[hour] for hour in hours)
            if best_total is None or total < best_total:  # strict "<" keeps earliest on ties
                best_start, best_total = start, total
        return best_start
