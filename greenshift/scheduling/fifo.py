"""FIFO baseline (GS-40): every job starts the moment it arrives.

This is how a conventional, carbon-unaware scheduler behaves with spare capacity. It
ignores the forecast entirely, which is the point: it is what we measure the
carbon-aware scheduler against.
"""

from __future__ import annotations

from typing import Sequence

import pandas as pd

from greenshift.scheduling.base import ScheduledJob
from greenshift.workload.jobs import Job


class FifoScheduler:
    name = "fifo"

    def schedule(self, jobs: Sequence[Job],
                 forecast: pd.Series | None = None) -> list[ScheduledJob]:
        ordered = sorted(jobs, key=lambda job: (job.arrival_utc, job.job_id))
        return [ScheduledJob(job=job, start_utc=job.arrival_utc) for job in ordered]
