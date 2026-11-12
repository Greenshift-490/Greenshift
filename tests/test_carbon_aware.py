"""Tests for GS-38 (delay flexible jobs) and GS-39 (respect deadlines)."""

import random
from datetime import date, timedelta

import pandas as pd
import pytest

from greenshift.carbon.approximation import SIGNAL_NAME, renewable_share_signal
from greenshift.data.oasis import pacific_day_range
from greenshift.scheduling import (
    CarbonAwareScheduler,
    DeadlineViolation,
    FifoScheduler,
    ScheduledJob,
)
from greenshift.scheduling.deadlines import check_deadlines, feasible_start_times
from greenshift.workload.jobs import Job, generate_jobs

START, END = pacific_day_range(date(2026, 10, 1))
HOURS = pd.date_range(START, END, freq="h", inclusive="left")
H = timedelta(hours=1)


def forecast(values: dict[int, float], default: float = 1.0) -> pd.Series:
    """Hourly forecast for the day; ``values`` overrides hour offsets from START."""
    series = pd.Series(default, index=HOURS)
    for offset, value in values.items():
        series.iloc[offset] = value
    return series


def job(arrival=0, duration=1, slack=5, flexible=True, job_id="j"):
    arrival_utc = START + arrival * H
    return Job(job_id, arrival_utc, duration,
               arrival_utc + (duration + slack) * H, 10.0, flexible)


# --- GS-39: deadlines -------------------------------------------------------------

def test_feasible_starts_run_from_arrival_to_latest_start():
    starts = feasible_start_times(job(arrival=2, duration=2, slack=3))
    assert starts == [START + h * H for h in (2, 3, 4, 5)]


def test_inflexible_job_has_only_one_feasible_start():
    assert feasible_start_times(job(slack=0, flexible=False)) == [START]


def test_check_deadlines_raises_on_late_job():
    late = ScheduledJob(job(slack=1), START + 2 * H)
    with pytest.raises(DeadlineViolation):
        check_deadlines([late])


def test_carbon_aware_never_misses_a_deadline():
    rng = random.Random(0)
    noisy = pd.Series([rng.random() for _ in HOURS], index=HOURS)
    jobs = generate_jobs(300, START, END, seed=5, flexible_share=1.0)
    schedule = CarbonAwareScheduler().schedule(jobs, noisy)
    assert all(s.meets_deadline for s in schedule)


# --- GS-38: delay flexible jobs ---------------------------------------------------

def test_flexible_job_moves_to_cleanest_hour():
    [s] = CarbonAwareScheduler().schedule([job(slack=5)], forecast({3: 0.2}))
    assert s.start_utc == START + 3 * H and s.delay_hours == 3


def test_multi_hour_job_uses_total_over_its_run():
    # Hour 1 alone is cleanest, but hours 3+4 together beat hours 1+2.
    f = forecast({1: 0.1, 2: 0.9, 3: 0.3, 4: 0.3})
    [s] = CarbonAwareScheduler().schedule([job(duration=2, slack=4)], f)
    assert s.start_utc == START + 3 * H


def test_cleaner_hour_after_deadline_is_ignored():
    [s] = CarbonAwareScheduler().schedule([job(slack=2)], forecast({2: 0.5, 10: 0.0}))
    assert s.start_utc == START + 2 * H


def test_inflexible_job_starts_on_arrival():
    [s] = CarbonAwareScheduler().schedule([job(slack=0, flexible=False)], forecast({1: 0.0}))
    assert s.delay_hours == 0


def test_ties_prefer_earliest_start():
    [s] = CarbonAwareScheduler().schedule([job(slack=5)], forecast({}))  # all equal
    assert s.delay_hours == 0


def test_forecast_gap_hours_are_skipped():
    f = forecast({3: 0.0, 4: 0.5})
    f.iloc[3] = float("nan")  # the "cleanest" hour is actually unknown
    [s] = CarbonAwareScheduler().schedule([job(slack=5)], f)
    assert s.start_utc == START + 4 * H


def test_no_forecast_data_means_no_delay():
    empty = pd.Series(float("nan"), index=HOURS)
    [s] = CarbonAwareScheduler().schedule([job(slack=5)], empty)
    assert s.delay_hours == 0


def test_forecast_is_required():
    with pytest.raises(ValueError):
        CarbonAwareScheduler().schedule([job()])


def test_never_dirtier_than_fifo_by_forecast():
    rng = random.Random(1)
    f = pd.Series([rng.random() for _ in HOURS], index=HOURS)
    jobs = generate_jobs(200, START, END, seed=9)

    def total(schedule):
        return sum(f[pd.Timestamp(s.start_utc + h * H)]
                   for s in schedule for h in range(s.job.duration_hours))

    assert total(CarbonAwareScheduler().schedule(jobs, f)) <= total(FifoScheduler().schedule(jobs))


# --- Approximate signal -----------------------------------------------------------

def test_renewable_share_signal():
    hourly = pd.DataFrame({
        "interval_start_utc": HOURS[:3],
        "demand_mw": [100.0, 100.0, 100.0],
        "solar_mw": [0.0, 60.0, float("nan")],
        "wind_mw": [10.0, 20.0, 10.0],
    })
    signal = renewable_share_signal(hourly)
    assert signal.name == SIGNAL_NAME  # labeled as an approximation
    assert signal.iloc[0] == pytest.approx(0.9)
    assert signal.iloc[1] == pytest.approx(0.2)
    assert pd.isna(signal.iloc[2])  # missing data stays missing
