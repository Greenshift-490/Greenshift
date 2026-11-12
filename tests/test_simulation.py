"""Tests for GS-32 (run workloads through schedulers) and GS-34 (calculate emissions)."""

from datetime import date, timedelta

import pandas as pd
import pytest

from greenshift.data.oasis import pacific_day_range
from greenshift.scheduling import CarbonAwareScheduler, FifoScheduler, ScheduledJob
from greenshift.simulation.emissions import compare, job_emissions
from greenshift.simulation.runner import run_schedulers
from greenshift.workload.jobs import Job, generate_jobs

START, END = pacific_day_range(date(2026, 10, 1))
HOURS = pd.date_range(START, END, freq="h", inclusive="left")
H = timedelta(hours=1)
METRIC = "caiso_avg_co2_t_per_mwh"


def series(values, name):
    return pd.Series(values, index=HOURS, name=name)


# A day where the forecast and the actual data agree that hours 10-14 are cleanest.
SHAPE = [0.1 if 10 <= h <= 14 else 0.3 for h in range(24)]
FORECAST = series(SHAPE, "approx_test_signal")
ACTUAL = series(SHAPE, METRIC)


def job(job_id="j", arrival=0, duration=2, slack=0, power=10.0):
    a = START + arrival * H
    return Job(job_id, a, duration, a + (duration + slack) * H, power, slack > 0)


# --- GS-32: running schedulers ----------------------------------------------------

def test_every_scheduler_gets_the_same_jobs():
    jobs = generate_jobs(40, START, END, seed=1)
    results = run_schedulers(jobs, [FifoScheduler(), CarbonAwareScheduler()], FORECAST)
    assert set(results) == {"fifo", "carbon_aware"}
    for schedule in results.values():
        assert sorted(s.job.job_id for s in schedule) == sorted(j.job_id for j in jobs)


def test_duplicate_scheduler_names_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        run_schedulers([job()], [FifoScheduler(), FifoScheduler()], FORECAST)


def test_scheduler_that_drops_a_job_is_rejected():
    class Broken:
        name = "broken"

        def schedule(self, jobs, forecast=None):
            return []

    with pytest.raises(ValueError, match="every job"):
        run_schedulers([job()], [Broken()], FORECAST)


# --- GS-34: emissions -------------------------------------------------------------

def test_emissions_are_power_times_intensity_per_hour():
    # 10 kW for 2 hours at 0.3 t/MWh: 10 * 0.3 + 10 * 0.3 = 6 kg
    [row] = job_emissions([ScheduledJob(job(), START)], ACTUAL, "fifo").to_dict("records")
    assert row["energy_kwh"] == 20
    assert row["emissions_kg"] == pytest.approx(6.0)
    assert row["metric"] == METRIC


def test_unnamed_intensity_is_rejected():
    with pytest.raises(ValueError, match="named"):
        job_emissions([], pd.Series(dtype=float), "fifo")


def test_job_in_a_data_gap_is_unscored_not_zero():
    gappy = ACTUAL.copy()
    gappy.iloc[1] = float("nan")
    [row] = job_emissions([ScheduledJob(job(), START)], gappy, "fifo").to_dict("records")
    assert not row["scored"]
    assert pd.isna(row["emissions_kg"])


def test_carbon_aware_beats_fifo_when_forecast_is_right():
    jobs = [job("a", arrival=6, duration=2, slack=8), job("b", arrival=0, duration=1)]
    schedules = run_schedulers(jobs, [FifoScheduler(), CarbonAwareScheduler()], FORECAST)
    _, summary = compare(schedules, ACTUAL)
    by_name = summary.set_index("scheduler")
    # Job a moves from 0.3 hours into 0.1 hours; job b is inflexible and stays.
    assert by_name.loc["fifo", "emissions_kg"] == pytest.approx(9.0)
    assert by_name.loc["carbon_aware", "emissions_kg"] == pytest.approx(5.0)
    assert by_name.loc["carbon_aware", "reduction_vs_baseline_pct"] == pytest.approx(44.44, 0.01)
    assert (summary["deadline_misses"] == 0).all()


def test_wrong_forecast_is_measured_honestly():
    # The forecast says hour 2 is clean, but in reality it is the dirtiest hour.
    forecast = series([0.0 if h == 2 else 0.5 for h in range(24)], "misleading_signal")
    actual = series([0.9 if h == 2 else 0.2 for h in range(24)], METRIC)
    schedules = run_schedulers([job(duration=1, slack=4)],
                               [FifoScheduler(), CarbonAwareScheduler()], forecast)
    _, summary = compare(schedules, actual)
    assert summary.set_index("scheduler").loc["carbon_aware", "reduction_vs_baseline_pct"] < 0


def test_comparison_uses_only_jobs_scored_under_every_scheduler():
    gappy = ACTUAL.copy()
    gappy.iloc[0] = float("nan")  # FIFO runs job "b" in this gap hour
    jobs = [job("a", arrival=6, duration=2, slack=8), job("b", arrival=0, duration=1)]
    schedules = run_schedulers(jobs, [FifoScheduler(), CarbonAwareScheduler()], FORECAST)
    _, summary = compare(schedules, gappy)
    assert (summary["jobs_compared"] == 1).all()
    assert (summary["jobs_unscored"] == 1).all()


def test_baseline_must_exist():
    schedules = run_schedulers([job()], [FifoScheduler()], FORECAST)
    with pytest.raises(ValueError, match="baseline"):
        compare(schedules, ACTUAL, baseline="nope")
