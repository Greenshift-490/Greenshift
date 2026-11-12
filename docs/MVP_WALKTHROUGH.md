# GreenShift MVP: What We Built and How It Works

This document explains the GreenShift proof of concept for the whole team: what it does,
the reasoning behind each part, the results so far, and how to run it yourself.

---

## 1. The MVP in one paragraph

GreenShift downloads real California grid data from CAISO, generates a reproducible set
of simulated cloud jobs, and schedules those jobs two ways: **FIFO** (run every job as
soon as it arrives) and **carbon-aware** (delay flexible jobs to cleaner hours without
missing their deadlines). It then measures how much CO2 each schedule *actually* caused,
using CAISO's real emissions data, and compares the two.

```
 CAISO OASIS (forecasts)         CAISO Today's Outlook (actuals)
   demand, solar, wind               CO2 emissions, demand
          │                                   │
          ▼                                   ▼
   hourly forecast table            hourly ACTUAL intensity (t CO2/MWh)
          │                                   │
          ▼                                   │
   approximate carbon signal                  │
          │                                   │
 jobs ──► FIFO scheduler ─────────┐           │
   │                              ├──► emissions calculation ──► comparison
   └────► carbon-aware scheduler ─┘     (scored with actuals)       + audit folder
```

## 2. First results

October 1, 2026, scored on CAISO's **actual average** emissions:

| Scheduler    | Jobs | Emissions (kg CO2) | Avg delay | Missed deadlines | Reduction |
|--------------|------|--------------------|-----------|------------------|-----------|
| FIFO         | 50   | 815.2              | 0 h       | 0                | baseline  |
| Carbon-aware | 50   | 707.6              | 1.4 h     | 0                | **13.2%** |

Repeating with 200 jobs and 5 different random seeds gave **10–16% lower emissions**,
always with zero missed deadlines.

**Caveats that must accompany these numbers in any report:**

- Only one day of data so far.
- Uses CAISO's *average* emissions rate, not *marginal* emissions (see section 4.3).
- Jobs are simulated, not real cloud workloads.
- Assumes unlimited capacity: any number of jobs can run in the same hour.

---

## 3. Core principles

These rules shaped every part of the code. If you're adding to the project, keep them.

1. **The scheduler only sees forecasts. Actual data is only used for scoring.**
   Otherwise the carbon-aware scheduler would be "cheating" by knowing the future, and the
   comparison with FIFO would be unfair.
2. **Never invent data.** Missing hours stay empty (NaN) and are reported, never filled in
   or treated as zero.
3. **Label every carbon number with what it is.** An approximation, an average rate and a
   marginal rate are different things and are never mixed.
4. **Everything is reproducible.** Jobs come from a fixed random seed, raw downloads are
   never overwritten, and every simulation run saves its inputs and outputs.
5. **Timestamps are always UTC inside the code.** Pacific time only exists at the edges
   (CAISO files and command-line dates), because daylight saving makes local time
   ambiguous.
6. **Modular parts.** Data sources, jobs, schedulers and evaluation don't depend on each
   other's internals, so we can add MIDAS, an API, a dashboard or Kubernetes later without
   rebuilding the scheduler.

---

## 4. Step by step

### 4.1 Downloading forecasts: CAISO OASIS (GS-16)

**File:** `greenshift/data/oasis.py`

OASIS is CAISO's public market data API. We request two reports as zipped CSVs:

| Report         | Contents                                       |
|----------------|------------------------------------------------|
| `SLD_FCST`     | Day-ahead demand forecast (MW)                 |
| `SLD_REN_FCST` | Day-ahead solar and wind forecasts (MW)        |

How it handles real-world problems:

- **Errors:** OASIS often reports errors with a "success" response containing an error
  XML file instead of data. We check for that and raise a clear error with CAISO's code.
- **Retries:** network failures, rate limiting (HTTP 429) and server errors (5xx) are
  retried with increasing waits. Bad requests (e.g. 400) fail immediately.
- **Politeness:** at least 5 seconds between requests; ranges longer than 31 days are
  split into separate requests.
- **Time zones:** dates are interpreted as California days (23, 24 or 25 hours long
  depending on daylight saving) and converted to UTC for the request.
- **Provenance:** every row records which report produced it and *when it was
  downloaded*. Each download is saved to a new file and old files are never overwritten,
  because forecasts change. This lets us replay exactly what the scheduler knew.

### 4.2 Cleaning and combining (GS-24)

**File:** `greenshift/data/combine.py`

Real downloads contained more than we needed:

- `SLD_FCST` covers ~36 areas, including Western states in the Energy Imbalance Market.
  We keep only **`CA ISO-TAC`**, the CAISO total. Sub-areas such as `PGE-TAC` are already
  inside that total, so adding them would double-count.
- `SLD_REN_FCST` reports solar and wind per trading hub. We add up CAISO's hubs
  (**NP15, SP15, ZP26**) and drop the rest (e.g. `AVA_`).

The result is one row per hour: `demand_mw`, `solar_mw`, `wind_mw`, plus the download
time of each source. Duplicate hours are an error; missing hours are kept as gaps.

### 4.3 Carbon data: three different numbers

This is the most important concept in the project.

| Signal | Source | What it measures | Used for |
|---|---|---|---|
| `approx_carbon_index_renewable_share` | OASIS forecasts | 1 − (solar + wind) / demand. Unitless, 0 = clean, 1 = dirty | **Scheduling** (MVP) |
| MIDAS marginal forecast | CEC MIDAS 2.0 API | CO2 from the power plant that responds to *extra* demand | **Scheduling** (planned) |
| `caiso_avg_co2_t_per_mwh` | CAISO Today's Outlook | Actual total CO2 ÷ actual demand, in tonnes per MWh | **Scoring only** |

**Approximation (`greenshift/carbon/approximation.py`).** When solar and wind cover more
of the demand, the grid is usually cleaner. This is good enough to *rank* hours, which is
all the scheduler needs, but it is not an emissions number. It ignores imports, hydro,
nuclear and batteries. It is named as an approximation everywhere it appears.

**Average vs. marginal.** The *average* rate is total emissions divided by total demand.
The *marginal* rate is what an *additional* unit of demand would emit. That depends on
which plant ramps up to meet it, usually natural gas. Marginal is arguably the better
signal for deciding *when* to run a job. We keep both, and never combine them.

**Actual emissions (GS-44, `greenshift/data/outlook.py`).** CAISO publishes CO2 (tonnes
per hour, by source) and demand every 5 minutes at
`https://www.caiso.com/outlook/history/YYYYMMDD/{co2,demand}.csv`. We average each hour
and divide CO2 by demand. CAISO itself calls its CO2 data an approximation. Two quirks we
found in the real files:

- `demand.csv` ends with an extra `00:00` row that belongs to the next day, so we drop it.
- On the November "fall back" day, 1 am happens twice, but CAISO lists 01:00–01:55 only
  once. We can't tell which 1 am those rows belong to, so both 1 am hours become gaps
  (configurable).

An hour only gets an intensity value if all twelve 5-minute readings are present.

### 4.4 Simulated jobs (GS-28)

**File:** `greenshift/workload/jobs.py`

Each job has:

| Field | Meaning |
|---|---|
| `arrival_utc` | When the job is submitted (on the hour) |
| `duration_hours` | How long it runs (1–4 h by default) |
| `deadline_utc` | When it must be finished |
| `power_kw` | Constant power draw (10–50 kW by default) |
| `flexible` | Whether it may be delayed (60% of jobs by default) |

**Slack** = how many hours a job can wait and still meet its deadline. Inflexible jobs
have zero slack; flexible jobs get 4–12 hours. Slack is what the carbon-aware scheduler
uses to reduce emissions.

Design choices:

- **Whole hours**, to match the hourly CAISO data.
- **Fixed random seed:** the same seed always produces the same jobs, so both schedulers
  are compared on identical workloads and teammates can reproduce results.
- **Jobs never extend past the end of the time window**, since we'd have no carbon data
  to score them.

### 4.5 Schedulers (GS-40, GS-38, GS-39)

**Files:** `greenshift/scheduling/`

All schedulers follow one interface (`base.py`):

```python
schedule(jobs, forecast) -> list[ScheduledJob]   # ScheduledJob = job + start time
```

The forecast is the only carbon information a scheduler receives.

**FIFO (`fifo.py`, GS-40).** Every job starts the moment it arrives. This is how a
carbon-unaware scheduler behaves, which makes it our baseline.

**Deadlines (`deadlines.py`, GS-39).** Two safeguards:

1. `feasible_start_times(job)` lists only start hours that still finish on time, so a
   late start is never even considered.
2. `check_deadlines(schedule)` re-checks a finished schedule and raises an error if any
   job is late. A bug fails loudly instead of producing a fake "carbon saving".

**Carbon-aware (`carbon_aware.py`, GS-38).** For each flexible job, it tries every
feasible start time, adds up the forecast over the hours the job would run, and picks the
lowest total. Inflexible jobs start on arrival.

*Worked example.* A 2-hour flexible job arrives at hour 0 with 4 hours of slack. The
forecast for hours 0–5 is `0.8, 0.6, 0.3, 0.2, 0.2, 0.7`:

| Start | Hours run | Forecast total |
|---|---|---|
| 0 | 0, 1 | 1.4 |
| 1 | 1, 2 | 0.9 |
| 2 | 2, 3 | 0.5 |
| **3** | **3, 4** | **0.4 ← chosen** |
| 4 | 4, 5 | 0.9 |

The job starts at hour 3 and finishes at hour 5, well within its deadline of hour 6.

Rules worth knowing:

- **Ties keep the earliest start.** A job only waits if waiting is actually cleaner.
- **Forecast gaps are skipped**, never guessed. With no usable forecast, the job runs on
  arrival.
- Power draw doesn't affect *which* hour is cleanest, only the emissions total later.

### 4.6 Simulation and emissions (GS-32, GS-34)

**Files:** `greenshift/simulation/`

**Running (`runner.py`, GS-32).** Every scheduler receives the same jobs and the same
forecast. The runner rejects a scheduler that drops or duplicates a job, and re-checks
deadlines.

**Emissions (`emissions.py`, GS-34).** For each hour a job runs:

```
emissions_kg = power_kw × actual intensity (t CO2/MWh)
```

Why that works: P kW for one hour is P/1000 MWh, and P/1000 MWh × I t/MWh = P × I / 1000
tonnes = **P × I kilograms**.

Fairness rules:

- A job that runs during an hour without actual data is **unscored**, never counted as
  zero.
- Schedulers are compared **only on jobs scored under every scheduler**, so neither one
  benefits from its jobs landing in data gaps.
- A wrong forecast shows up as a worse result. One test deliberately misleads the
  scheduler and checks that it is reported as worse than FIFO.

**Audit trail (`run.py`).** Every run creates `data/processed/runs/<timestamp>/` with:

| File | Contents |
|---|---|
| `settings.json` | All settings, including seed and which signals were used |
| `jobs.csv` | The exact jobs generated |
| `forecast_used.csv` | The exact forecast the schedulers saw |
| `per_job.csv` | Each job's start time, delay and emissions, per scheduler |
| `summary.csv` | The comparison table |

---

## 5. Assumptions and limitations

| Assumption | Why | Possible next step |
|---|---|---|
| Unlimited capacity | Isolates the effect of *timing* | Add a capacity limit; FIFO then becomes a real queue |
| Hourly resolution | Matches CAISO forecast data | 5- or 15-minute scheduling |
| Constant power per job | Keeps emissions maths simple | Power profiles per job type |
| Simulated jobs | No real workload traces yet | Model jobs on real traces (e.g. AI training, batch analytics) |
| Renewable-share approximation for scheduling | Available from OASIS today | MIDAS marginal forecast |
| Single region (CAISO) | Our data source | Multiple regions; moving jobs between locations |

---

## 6. Try it yourself

### 6.1 One-time setup

Requires Python 3.11+ and Git.

**Windows (PowerShell):**

```powershell
git clone https://github.com/Greenshift-490/Greenshift.git
cd Greenshift
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

If activation is blocked, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

**macOS / Linux:**

```bash
git clone https://github.com/Greenshift-490/Greenshift.git
cd Greenshift
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

All 83 tests should pass. They don't need internet access.

### 6.2 Run the full pipeline for one day

Run these from the repo folder with the virtual environment activated.

**Step 1: download the forecasts**

```bash
python -m greenshift.data.oasis --query SLD_FCST --market DAM --day 2026-10-01
python -m greenshift.data.oasis --query SLD_REN_FCST --market DAM --day 2026-10-01
```

Each command prints the file it saved in `data/raw/`.

**Step 2: combine them into one hourly table**

Use the two filenames printed in step 1. Tab-completion helps.

```bash
python -m greenshift.data.combine --demand data/raw/SLD_FCST_DAM_<...>.csv --renewables data/raw/SLD_REN_FCST_DAM_<...>.csv
```

This writes `data/processed/caiso_hourly.csv`.

**Step 3: download actual emissions**

```bash
python -m greenshift.data.outlook --day 2026-10-01
```

This writes `data/processed/caiso_actual_emissions.csv`.

**Step 4: run the comparison**

```bash
python -m greenshift.simulation.run --day 2026-10-01
```

It prints the comparison table and saves an audit folder in `data/processed/runs/`.

### 6.3 Experiments to try

```bash
# More jobs, different random workload
python -m greenshift.simulation.run --day 2026-10-01 --jobs 200 --seed 7

# What if only 20% of jobs are flexible? Or all of them?
python -m greenshift.simulation.run --day 2026-10-01 --flexible-share 0.2
python -m greenshift.simulation.run --day 2026-10-01 --flexible-share 1.0

# Just look at generated jobs
python -m greenshift.workload.jobs --day 2026-10-01 --count 20
```

For a multi-day window, pass `--days N` to the download commands in steps 1 and 3 and to
the simulation, using the same start day.

Other things worth exploring:

- Open `per_job.csv` in a run folder and filter to `delay_hours > 0` to see which jobs
  moved and to when.
- Compare `forecast_used.csv` with `caiso_actual_emissions.csv`: how well does the
  approximation rank the hours?
- Try a winter day, a cloudy day, or the November 1 fall-back day.

### 6.4 Where data goes

`data/raw/` and `data/processed/` are ignored by Git. Data is never committed; everyone
downloads their own copy with the commands above, so the repo stays small and results stay
reproducible.

---

## 7. Repository layout

```
greenshift/
  data/
    oasis.py            GS-16  download OASIS forecasts
    combine.py          GS-24  clean + combine into an hourly table
    outlook.py          GS-44  download actual CO2 + demand
  carbon/
    approximation.py           renewable-share approximation (scheduling signal)
  workload/
    jobs.py             GS-28  simulated jobs
  scheduling/
    base.py                    shared scheduler interface
    fifo.py             GS-40  FIFO baseline
    deadlines.py        GS-39  deadline safeguards
    carbon_aware.py     GS-38  carbon-aware scheduler
  simulation/
    runner.py           GS-32  run jobs through schedulers
    emissions.py        GS-34  score schedules with actual emissions
    run.py                     end-to-end command
tests/                         one test file per module (83 tests)
```

## 8. Jira map

| Story | Title | Status in code |
|---|---|---|
| GS-15 | Set up the Python Environment | Done |
| GS-16 | Import Historical Carbon Data | Done (OASIS forecasts) |
| GS-24 | Clean and Standardize Data | Done |
| GS-28 | Generate Simulated Cloud Jobs | Done (also covers the basics of GS-29 and GS-30) |
| GS-40 | Schedule Jobs Immediately | Done (FIFO) |
| GS-38 | Delay Flexible Jobs | Done |
| GS-39 | Respect Job Deadlines | Done |
| GS-44 | Import Actual Carbon Emissions Data | Done |
| GS-32 | Run Workloads Through a Scheduler | Done |
| GS-34 | Calculate Carbon Emissions | Done |

## 9. How we work in Git

- **One branch per Jira story**, named `feature/GS-<number>-<short-description>`. When
  two closely linked stories share a branch, both keys go in the branch name, PR title and
  commit title (e.g. `GS-38, GS-39: ...`).
- **Always branch from an up-to-date `main`:**
  ```bash
  git switch main
  git pull
  git switch -c feature/GS-<number>-<description>
  ```
  Don't branch from another feature branch. With squash merging that causes conflicts.
- **Commit messages start with the story key**, e.g. `GS-24: Clean OASIS data ...`.
- **Open a pull request into `main`** and have a teammate review it. Use **Squash and
  merge** so `main` keeps one commit per story.
- **Git won't switch branches?** You have uncommitted changes to a tracked file. Use
  `git stash`, switch, then `git stash pop`.
- **"LF will be replaced by CRLF" warnings** on Windows are harmless line-ending
  conversions.

## 10. What's next

1. **MIDAS marginal emissions forecast** as a second scheduling signal, reported
   separately from CAISO average emissions.
2. **API** so GreenShift runs as a service (submit jobs, get schedules and results).
3. **Dashboard** to visualize schedules, carbon signals and savings.
4. **Cloud deployment** of the service.
5. **Kubernetes integration** (next semester): a Kubernetes adapter submits each
   `ScheduledJob` at its chosen start time. The schedulers don't need to change.
