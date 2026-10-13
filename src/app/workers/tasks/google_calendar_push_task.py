"""Pushes Appointments queued with google_calendar_sync_status=pending out to Google Calendar —
the (only) outbound half of the one-way sync (see services/google_calendar_sync_service.py).
Poll-based, not fired inline from appointment_service.py's book/reschedule/cancel, matching this
codebase's existing convention of never calling an external integration synchronously from an
admin-facing request.
"""

import asyncio

from app.core.config import settings
from app.core.logging import get_logger
from app.db.mongodb import close_db, connect_db
from app.repositories.appointment_repository import appointment_repository
from app.services.google_calendar_sync_service import google_calendar_sync_service
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def push_pending_google_calendar_appointments_once() -> int:
    """Core sweep logic — assumes the DB is already connected. Shared by the standalone Celery
    task below and by workers/inprocess_scheduler.py.
    """
    if not settings.google_calendar_id:
        logger.warning("google_calendar_push_skipped", reason="GOOGLE_CALENDAR_ID not configured")
        return 0
    pending = await appointment_repository.list_pending_google_calendar_sync()
    for appointment in pending:
        try:
            await google_calendar_sync_service.process_pending_appointment(appointment)
        except Exception:
            # process_pending_appointment already catches GoogleCalendarIntegrationError
            # internally; this outer guard is belt-and-suspenders against a genuinely unexpected
            # bug on one appointment taking down the whole sweep tick for every other pending row.
            logger.exception("google_calendar_push_unexpected_error", appointment_id=str(appointment.id))
    return len(pending)


async def _push_pending_google_calendar_appointments() -> int:
    """Standalone entry point for the Celery task — owns its own DB connection since it runs in
    a separate worker process from the API.
    """
    await connect_db()
    try:
        return await push_pending_google_calendar_appointments_once()
    finally:
        await close_db()


@celery_app.task(name="app.workers.tasks.google_calendar_push_task.push_pending_google_calendar_appointments")
def push_pending_google_calendar_appointments() -> int:
    return asyncio.run(_push_pending_google_calendar_appointments())
