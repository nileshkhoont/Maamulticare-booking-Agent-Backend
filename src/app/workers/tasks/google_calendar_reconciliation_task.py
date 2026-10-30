"""Reconciliation sweep for the Google Calendar push queue — un-sticks any Appointment whose last
push attempt failed, or has been stuck `syncing` longer than a crash could plausibly explain, back
to `pending` so the next push_task tick retries it.

This is a one-way, outbound-only integration — there is no inbound-catch-up pass at all, unlike a
two-way sync, since Google Calendar is never read back into this system.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from beanie.operators import Set

from app.core.constants import GoogleCalendarSyncStatus
from app.core.logging import get_logger
from app.db.mongodb import close_db, connect_db
from app.models.appointment import Appointment
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

STUCK_SYNCING_THRESHOLD_MINUTES = 15


async def reconcile_google_calendar_once() -> int:
    """Core sweep logic — assumes the DB is already connected. Shared by the standalone Celery
    task below and by workers/inprocess_scheduler.py.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=STUCK_SYNCING_THRESHOLD_MINUTES)
    failed = await Appointment.find(
        Appointment.google_calendar_sync_status == GoogleCalendarSyncStatus.failed,
        Appointment.is_deleted == False,  # noqa: E712
    ).to_list()
    stuck_syncing = await Appointment.find(
        Appointment.google_calendar_sync_status == GoogleCalendarSyncStatus.syncing,
        Appointment.updated_at < cutoff,
        Appointment.is_deleted == False,  # noqa: E712
    ).to_list()
    for appointment in [*failed, *stuck_syncing]:
        # $set-only update (not .save()) — never overwrite fields another concurrent sweep
        # (e.g. whatsapp_push_task) may be mid-write on this same row. See
        # google_calendar_sync_service.py's module docstring for the race this avoids.
        await appointment.update(Set({Appointment.google_calendar_sync_status: GoogleCalendarSyncStatus.pending}))
        logger.info("google_calendar_sync_unstuck", appointment_id=str(appointment.id))
    return len(failed) + len(stuck_syncing)


async def _reconcile_google_calendar() -> int:
    """Standalone entry point for the Celery task — owns its own DB connection since it runs in
    a separate worker process from the API.
    """
    await connect_db()
    try:
        return await reconcile_google_calendar_once()
    finally:
        await close_db()


@celery_app.task(name="app.workers.tasks.google_calendar_reconciliation_task.reconcile_google_calendar")
def reconcile_google_calendar() -> int:
    return asyncio.run(_reconcile_google_calendar())
