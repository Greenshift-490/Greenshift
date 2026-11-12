"""GS-32: Run workloads through a scheduler.

Every scheduler gets the exact same jobs and the exact same forecast, and nothing else.
Actual emissions are not an input here at all, which is what keeps the comparison fair.
"""

from __future__ import annotations

from typing import Sequence

import pandas as pd

from greenshift.scheduling import ScheduledJob, Scheduler
from greenshift.scheduling.deadlines import check_deadlines
from greenshift.workload.jobs import Job


def run_schedulers(jobs: Sequence[Job], schedulers: Sequence[Scheduler],
                   forecast: pd.Series) -> dict[str, list[ScheduledJob]]:
    """Return {scheduler name: schedule}. Raises if any schedule misses a deadline."""
    names = [s.name for s in schedulers]
    if len(set(names)) != len(names):
        raise ValueError(f"Scheduler names must be unique, got {names}")

    jobs = tuple(jobs)  # frozen Jobs in a tuple: no scheduler can change the workload
    results = {}
    for scheduler in schedulers:
        schedule = scheduler.schedule(jobs, forecast)
        if sorted(s.job.job_id for s in schedule) != sorted(j.job_id for j in jobs):
            raise ValueError(f"{scheduler.name} did not schedule every job exactly once")
        check_deadlines(schedule)
        results[scheduler.name] = schedule
    return results
