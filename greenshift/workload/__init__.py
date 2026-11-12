"""Workloads: the jobs GreenShift schedules.

Jobs are plain data with no knowledge of schedulers, carbon data or Kubernetes, so the
same jobs can be fed to FIFO, the carbon-aware scheduler, or a real cluster later.
"""
