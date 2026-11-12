"""GS-39: Respect job deadlines.

Two safeguards:
1. ``feasible_start_times`` lists only the start hours that still finish by the deadline.
   Schedulers choose from this list, so a late start is never even considered.
2. ``check_deadlines`` double-checks a finished schedule and raises if any job would
   miss its deadline. A bug in a scheduler therefore fails loudly instead of quietly
   producing a "carbon saving" that broke a deadline.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from greenshift.scheduling.base import ScheduledJob
from greenshift.workload.jobs import Job


class DeadlineViolation(Exception):
    """A schedule would make at least one job miss its deadline."""


def feasible_start_times(job: Job) -> list[datetime]:
    """Every whole-hour start from arrival to the latest start, inclusive."""
    return [job.arrival_utc + timedelta(hours=h) for h in range(job.slack_hours + 1)]


def check_deadlines(schedule: Sequence[ScheduledJob]) -> None:
    late = [s.job.job_id for s in schedule if not s.meets_deadline]
    if late:
        raise DeadlineViolation(f"{len(late)} job(s) miss their deadline: {late[:5]}")
