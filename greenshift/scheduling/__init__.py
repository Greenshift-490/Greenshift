"""Schedulers decide *when* each job starts. They do not run jobs.

Every scheduler follows the ``Scheduler`` interface in ``base.py``, so the simulation,
API and dashboard can swap FIFO for the carbon-aware scheduler (or a future Kubernetes
adapter can consume either) without changes.
"""

from greenshift.scheduling.base import ScheduledJob, Scheduler, schedule_to_frame
from greenshift.scheduling.fifo import FifoScheduler

__all__ = ["ScheduledJob", "Scheduler", "schedule_to_frame", "FifoScheduler"]
