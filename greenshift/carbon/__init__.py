"""Carbon signals: hourly series the scheduler uses to compare time slots.

Each signal is a pandas Series indexed by hour start (UTC). Lower values mean cleaner
electricity. Every signal's ``name`` says exactly what it is, so an approximation can
never be mistaken for a measured emissions rate in results or on the dashboard.

Planned signals:
* ``approximation.py``  renewable-share approximation from CAISO OASIS (this MVP)
* ``midas.py``          MIDAS marginal emissions forecast (later)
* CAISO average emissions (later, for evaluation only, never shown to schedulers)
"""
