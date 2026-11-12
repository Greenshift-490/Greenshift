"""Simulated cloud jobs (GS-28).

A job arrives at some hour, needs ``duration_hours`` of compute at ``power_kw``, and must
finish by ``deadline_utc``. The gap between "earliest possible finish" and the deadline
is the job's *slack*: how long it could wait for cleaner electricity.

* Inflexible jobs (``flexible=False``) have zero slack and must run on arrival.
* Flexible jobs get a few to many hours of slack.

Design choices for the MVP:
* Whole hours only. Our CAISO data is hourly, so hour-aligned jobs map cleanly onto
  carbon intensity values. Finer resolution can come later.
* Seeded randomness. The same seed always produces the same jobs, so FIFO and the
  carbon-aware scheduler are compared on identical workloads, and anyone on the team
  can reproduce a result.
* Jobs never extend past the end of the time window, because we would have no carbon
  data to evaluate them with.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from greenshift.data.oasis import pacific_day_range

HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Job:
    job_id: str
    arrival_utc: datetime
    duration_hours: int
    deadline_utc: datetime
    power_kw: float
    flexible: bool

    def __post_init__(self):
        for name in ("arrival_utc", "deadline_utc"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{self.job_id}: {name} must be timezone-aware")
        if self.duration_hours < 1:
            raise ValueError(f"{self.job_id}: duration_hours must be at least 1")
        if self.power_kw <= 0:
            raise ValueError(f"{self.job_id}: power_kw must be positive")
        if self.deadline_utc < self.earliest_finish_utc:
            raise ValueError(f"{self.job_id}: deadline is before the earliest possible finish")

    @property
    def earliest_finish_utc(self) -> datetime:
        return self.arrival_utc + self.duration_hours * HOUR

    @property
    def latest_start_utc(self) -> datetime:
        """Starting any later than this misses the deadline."""
        return self.deadline_utc - self.duration_hours * HOUR

    @property
    def slack_hours(self) -> int:
        return int((self.latest_start_utc - self.arrival_utc) / HOUR)


def generate_jobs(count: int, window_start: datetime, window_end: datetime, seed: int,
                  flexible_share: float = 0.6, duration_range: tuple[int, int] = (1, 4),
                  slack_range: tuple[int, int] = (4, 12),
                  power_range_kw: tuple[float, float] = (10.0, 50.0)) -> list[Job]:
    """Generate ``count`` reproducible jobs that fit inside [window_start, window_end).

    ``flexible_share`` is the fraction of jobs that may be delayed. The ranges are
    inclusive bounds for duration (hours), slack for flexible jobs (hours) and power (kW).
    """
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("window_start and window_end must be timezone-aware")
    if not 0 <= flexible_share <= 1:
        raise ValueError("flexible_share must be between 0 and 1")
    window_hours = int((window_end - window_start) / HOUR)
    if window_hours < duration_range[1]:
        raise ValueError("Window is shorter than the longest possible job")

    rng = random.Random(seed)
    jobs = []
    for number in range(count):
        duration = rng.randint(*duration_range)
        # Latest arrival that still lets the job finish inside the window.
        arrival = window_start + rng.randint(0, window_hours - duration) * HOUR
        flexible = rng.random() < flexible_share
        hours_left = int((window_end - arrival) / HOUR) - duration
        slack = min(rng.randint(*slack_range), hours_left) if flexible else 0
        jobs.append(Job(
            job_id=f"job-{number:04d}",
            arrival_utc=arrival,
            duration_hours=duration,
            deadline_utc=arrival + (duration + slack) * HOUR,
            power_kw=round(rng.uniform(*power_range_kw), 2),
            flexible=flexible,
        ))
    return sorted(jobs, key=lambda job: (job.arrival_utc, job.job_id))


def jobs_to_frame(jobs: list[Job]) -> pd.DataFrame:
    df = pd.DataFrame([asdict(job) for job in jobs])
    df["slack_hours"] = [job.slack_hours for job in jobs]
    return df


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate simulated cloud jobs.")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--day", required=True, type=date.fromisoformat,
                        help="first Pacific-time day of the window, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--flexible-share", type=float, default=0.6)
    parser.add_argument("--out", default="data/processed/jobs.csv")
    args = parser.parse_args(argv)

    start, end = pacific_day_range(args.day, args.days)
    jobs = generate_jobs(args.count, start, end, args.seed, args.flexible_share)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    jobs_to_frame(jobs).to_csv(out, index=False)
    flexible = sum(job.flexible for job in jobs)
    print(f"Saved {len(jobs)} jobs ({flexible} flexible) to {out}")


if __name__ == "__main__":
    main()
