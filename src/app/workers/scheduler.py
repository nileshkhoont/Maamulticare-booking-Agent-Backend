"""Celery Beat schedule — periodically polls call_schedules for due entries and runs the safety
sweeps. Run with: `celery -A app.workers.celery_app beat -l info` alongside a worker
(`celery -A app.workers.celery_app worker -l info`).
"""

from app.core.config import settings
from app.workers.celery_app import celery_app

celery_app.conf.beat_schedule = {
    "dispatch-due-outbound-calls": {
        "task": "app.workers.tasks.outbound_call_task.dispatch_due_calls",
        "schedule": settings.outbound_call_poll_interval_seconds,
    },
    "sweep-stuck-in-progress-schedules": {
        "task": "app.workers.tasks.missed_call_retry_task.sweep_stuck_schedules",
        "schedule": 600.0,  # every 10 minutes
    },
    "backfill-missing-call-recordings": {
        "task": "app.workers.tasks.recording_backfill_task.backfill_missing_recordings",
        "schedule": 300.0,  # every 5 minutes
    },
}
