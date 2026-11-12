"""APPROXIMATE carbon signal from CAISO OASIS forecasts. Not an emissions rate.

Idea: the larger the share of demand met by solar and wind, the cleaner the grid tends
to be. So we use

    approx_carbon_index = 1 - (solar_mw + wind_mw) / demand_mw

It is unitless, between 0 (all demand met by solar and wind) and 1 (none). It is useful
for *ranking* hours, which is all the scheduler needs, but it says nothing about tonnes
of CO2. It ignores imports, hydro, nuclear, batteries and which plants are on the margin.
It will be replaced by the MIDAS marginal emissions forecast.
"""

from __future__ import annotations

import pandas as pd

SIGNAL_NAME = "approx_carbon_index_renewable_share"


def renewable_share_signal(hourly: pd.DataFrame) -> pd.Series:
    """Build the approximate signal from the GS-24 hourly table.

    Missing hours stay NaN; the scheduler treats them as unknown and won't pick them.
    """
    share = (hourly["solar_mw"] + hourly["wind_mw"]) / hourly["demand_mw"]
    signal = (1 - share).clip(lower=0, upper=1)
    signal.index = pd.DatetimeIndex(hourly["interval_start_utc"])
    signal.name = SIGNAL_NAME
    return signal.sort_index()
