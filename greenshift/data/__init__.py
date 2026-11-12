"""Data sources for GreenShift.

Each source lives in its own module (``oasis.py`` now, ``midas.py`` later) and returns a
pandas DataFrame with UTC timestamp columns plus provenance columns describing where and
when the data was retrieved. The scheduler never talks to an API directly.
"""
