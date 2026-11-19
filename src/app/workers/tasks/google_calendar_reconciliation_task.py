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
    # "Stuck" is measured from when the push actually started (google_calendar_sync_started_at),
    # never from updated_at — updated_at is only set at creation, so any appointment older than
    # the threshold used to look stuck the instant a push began, and got pushed a second time
    # while the first was still in flight. Rows already syncing at deploy time have no start
    # stamp, so only those fall back to the old updated_at comparison.
    stuck_syncing = await Appointment.find(
        Appointment.google_calendar_sync_status == GoogleCalendarSyncStatus.syncing,
        Appointment.is_deleted == False,  # noqa: E712
        {
            "$or": [
                {"google_calendar_sync_started_at": {"$lt": cutoff}},
                {"google_calendar_sync_started_at": None, "updated_at": {"$lt": cutoff}},
            ]
        },
    ).to_list()

    reset = 0
    for appointment in failed:
        reset += await _reset_to_pending(appointment, Appointment.google_calendar_sync_status == GoogleCalendarSyncStatus.failed)
    for appointment in stuck_syncing:
        reset += await _reset_to_pending(
            appointment,
            Appointment.google_calendar_sync_status == GoogleCalendarSyncStatus.syncing,
            Appointment.google_calendar_sync_started_at == appointment.google_calendar_sync_started_at,
        )
    return reset


async def _reset_to_pending(appointment: Appointment, *still_in_state) -> int:
    """Conditional $set: only resets the row if it is STILL in the state this sweep read it in, so a
    push that finished (or was re-claimed) after the read is never knocked back to pending.
    """
    result = await Appointment.find_one(Appointment.id == appointment.id, *still_in_state).update(
        Set({Appointment.google_calendar_sync_status: GoogleCalendarSyncStatus.pending})
    )
    if result.modified_count == 0:
        return 0
    logger.info("google_calendar_sync_unstuck", appointment_id=str(appointment.id))
    return 1


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
