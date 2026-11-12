from datetime import date, timedelta

import pandas as pd

from greenshift.data.oasis import pacific_day_range
from greenshift.scheduling import FifoScheduler, ScheduledJob, schedule_to_frame
from greenshift.workload.jobs import Job, generate_jobs

START, END = pacific_day_range(date(2026, 10, 1))
JOBS = generate_jobs(100, START, END, seed=3)


def test_every_job_starts_on_arrival():
    schedule = FifoScheduler().schedule(JOBS)
    assert len(schedule) == len(JOBS)
    assert all(s.start_utc == s.job.arrival_utc and s.delay_hours == 0 for s in schedule)


def test_fifo_always_meets_deadlines():
    # Deadlines are never before arrival + duration, so starting immediately always fits.
    assert all(s.meets_deadline for s in FifoScheduler().schedule(JOBS))


def test_fifo_ignores_the_forecast():
    forecast = pd.Series(1.0, index=pd.date_range(START, END, freq="h", inclusive="left"))
    scheduler = FifoScheduler()
    assert scheduler.schedule(JOBS, forecast) == scheduler.schedule(JOBS)


def test_output_is_in_arrival_order_regardless_of_input_order():
    schedule = FifoScheduler().schedule(list(reversed(JOBS)))
    arrivals = [s.job.arrival_utc for s in schedule]
    assert arrivals == sorted(arrivals)


def test_empty_workload_gives_empty_schedule():
    assert FifoScheduler().schedule([]) == []


# --- ScheduledJob, shared by all schedulers ---------------------------------------

def make_job():
    return Job("j", START, 2, START + timedelta(hours=5), 10.0, True)


def test_scheduled_job_derived_values():
    s = ScheduledJob(make_job(), START + timedelta(hours=3))
    assert s.end_utc == START + timedelta(hours=5)
    assert s.delay_hours == 3
    assert s.meets_deadline


def test_scheduled_job_detects_missed_deadline():
    assert not ScheduledJob(make_job(), START + timedelta(hours=4)).meets_deadline


def test_schedule_to_frame():
    df = schedule_to_frame(FifoScheduler().schedule(JOBS[:5]), "fifo")
    assert len(df) == 5
    assert (df["scheduler"] == "fifo").all()
    assert {"start_utc", "end_utc", "delay_hours", "meets_deadline"} <= set(df.columns)
