# Greenshift

A carbon-aware scheduler that runs flexible cloud workloads during cleaner-energy periods
while still meeting their deadlines, and compares the results against a FIFO baseline.

## Setup

Requires Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

`pip install -e` installs the `greenshift` package in "editable" mode, so code changes take
effect without reinstalling.

## Project layout

```
greenshift/     the Python package (all source code lives here)
tests/          pytest tests, one file per module
```

## Contributing

- One branch per Jira story, named `feature/GS-<number>-<short-description>`.
- Start commit messages with the story key, e.g. `GS-16: Add CAISO downloader`.
- Open a pull request into `main`; at least one teammate reviews before merging.
