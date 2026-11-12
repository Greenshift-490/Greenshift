from datetime import date, datetime, timedelta, timezone

import pytest

from greenshift.data.oasis import pacific_day_range
from greenshift.workload.jobs import Job, generate_jobs, jobs_to_frame

UTC = timezone.utc
START, END = pacific_day_range(date(2026, 10, 1))  # 24 hours


def make(n=200, seed=1, **kwargs):
    return generate_jobs(n, START, END, seed, **kwargs)


# --- Reproducibility --------------------------------------------------------------

def test_same_seed_gives_identical_jobs():
    assert make(seed=7) == make(seed=7)


def test_different_seed_gives_different_jobs():
    assert make(seed=7) != make(seed=8)


# --- Every generated job is valid -------------------------------------------------

def test_jobs_fit_inside_the_window():
    for job in make():
        assert START <= job.arrival_utc
        assert job.deadline_utc <= END


def test_jobs_are_hour_aligned_and_sorted_by_arrival():
    jobs = make()
    assert all(job.arrival_utc.minute == 0 and job.arrival_utc.second == 0 for job in jobs)
    assert [j.arrival_utc for j in jobs] == sorted(j.arrival_utc for j in jobs)


def test_values_stay_within_ranges():
    for job in make(duration_range=(2, 3), power_range_kw=(10, 20)):
        assert 2 <= job.duration_hours <= 3
        assert 10 <= job.power_kw <= 20


def test_inflexible_jobs_have_no_slack():
    jobs = make(flexible_share=0.0)
    assert all(not job.flexible and job.slack_hours == 0 for job in jobs)


def test_flexible_jobs_have_slack_unless_window_ends():
    jobs = make(flexible_share=1.0)
    assert all(job.flexible for job in jobs)
    # Slack is only cut short when the job would otherwise run past the window.
    for job in jobs:
        if job.slack_hours < 4:
            assert job.deadline_utc == END


def test_flexible_share_is_roughly_respected():
    jobs = make(n=1000, flexible_share=0.6)
    assert 0.55 < sum(job.flexible for job in jobs) / len(jobs) < 0.65


# --- Job validation ---------------------------------------------------------------

def job(**overrides):
    fields = dict(job_id="j", arrival_utc=START, duration_hours=2,
                  deadline_utc=START + timedelta(hours=5), power_kw=10.0, flexible=True)
    return Job(**(fields | overrides))


def test_job_derived_times():
    j = job()
    assert j.earliest_finish_utc == START + timedelta(hours=2)
    assert j.latest_start_utc == START + timedelta(hours=3)
    assert j.slack_hours == 3


@pytest.mark.parametrize("overrides", [
    {"arrival_utc": datetime(2026, 10, 1, 7)},                 # naive time
    {"duration_hours": 0},
    {"power_kw": 0},
    {"deadline_utc": START + timedelta(hours=1)},              # before earliest finish
])
def test_invalid_jobs_are_rejected(overrides):
    with pytest.raises(ValueError):
        job(**overrides)


def test_window_must_be_timezone_aware():
    with pytest.raises(ValueError):
        generate_jobs(5, datetime(2026, 10, 1), datetime(2026, 10, 2), seed=1)


# --- Export -----------------------------------------------------------------------

def test_jobs_to_frame_has_one_row_per_job():
    df = jobs_to_frame(make(n=10))
    assert len(df) == 10
    assert {"job_id", "arrival_utc", "deadline_utc", "slack_hours"} <= set(df.columns)
