"""The interface shared by all schedulers.

A scheduler receives:
* ``jobs``: the workload, and
* ``forecast``: the carbon-intensity *forecast* indexed by hour (UTC). This is the only
  carbon information a scheduler may see. Actual emissions are used afterwards, by the
  evaluation step, never by the scheduler. That separation keeps the FIFO comparison fair.

It returns one ``ScheduledJob`` per job: the job plus the chosen start time.

MVP assumption: unlimited capacity, so any number of jobs can run in the same hour. Both
schedulers share this assumption, so the comparison isolates the effect of *timing*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, Sequence

import pandas as pd

from greenshift.workload.jobs import Job


@dataclass(frozen=True)
class ScheduledJob:
    job: Job
    start_utc: datetime

    @property
    def end_utc(self) -> datetime:
        return self.start_utc + timedelta(hours=self.job.duration_hours)

    @property
    def delay_hours(self) -> int:
        return int((self.start_utc - self.job.arrival_utc) / timedelta(hours=1))

    @property
    def meets_deadline(self) -> bool:
        return self.end_utc <= self.job.deadline_utc


class Scheduler(Protocol):
    name: str

    def schedule(self, jobs: Sequence[Job],
                 forecast: pd.Series | None = None) -> list[ScheduledJob]:
        ...


def schedule_to_frame(schedule: Sequence[ScheduledJob], scheduler_name: str) -> pd.DataFrame:
    """One row per job, for saving, plotting or comparing schedulers."""
    return pd.DataFrame([{
        "scheduler": scheduler_name,
        "job_id": s.job.job_id,
        "arrival_utc": s.job.arrival_utc,
        "start_utc": s.start_utc,
        "end_utc": s.end_utc,
        "deadline_utc": s.job.deadline_utc,
        "delay_hours": s.delay_hours,
        "meets_deadline": s.meets_deadline,
        "duration_hours": s.job.duration_hours,
        "power_kw": s.job.power_kw,
        "flexible": s.job.flexible,
    } for s in schedule])
