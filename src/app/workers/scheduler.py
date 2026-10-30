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
    "push-pending-google-calendar-appointments": {
        "task": "app.workers.tasks.google_calendar_push_task.push_pending_google_calendar_appointments",
        "schedule": settings.google_calendar_push_poll_interval_seconds,
    },
    "reconcile-google-calendar": {
        "task": "app.workers.tasks.google_calendar_reconciliation_task.reconcile_google_calendar",
        "schedule": 600.0,  # every 10 minutes — mirrors the stuck-schedule sweep's cadence
    },
    "push-pending-whatsapp-notifications": {
        "task": "app.workers.tasks.whatsapp_push_task.push_pending_whatsapp_notifications",
        "schedule": settings.meta_whatsapp_push_poll_interval_seconds,
    },
    "reconcile-whatsapp-notifications": {
        "task": "app.workers.tasks.whatsapp_reconciliation_task.reconcile_whatsapp_notifications",
        "schedule": 600.0,  # every 10 minutes — mirrors Google Calendar's reconciliation cadence
    },
}
