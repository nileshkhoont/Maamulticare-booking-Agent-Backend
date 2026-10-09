"""Pushes Appointments queued with calendly_sync_status=pending out to Calendly — the outbound
half of the two-way sync (see services/calendly_sync_service.py). Poll-based, not fired inline
from appointment_service.py's book/reschedule/cancel, matching this codebase's existing
convention of never calling an external integration synchronously from an admin-facing request
(see outbound_call_task.py for the same pattern on the unrelated Edesy outbound-call queue).
"""

import asyncio

from app.core.logging import get_logger
from app.db.mongodb import close_db, connect_db
from app.repositories.appointment_repository import appointment_repository
from app.services.calendly_sync_service import calendly_sync_service
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def push_pending_calendly_appointments_once() -> int:
    """Core sweep logic — assumes the DB is already connected. Shared by the standalone Celery
    task below and by workers/inprocess_scheduler.py.
    """
    pending = await appointment_repository.list_pending_calendly_sync()
    for appointment in pending:
        await calendly_sync_service.process_pending_appointment(appointment)
    return len(pending)


async def _push_pending_calendly_appointments() -> int:
    await connect_db()
    try:
        return await push_pending_calendly_appointments_once()
    finally:
        await close_db()


@celery_app.task(name="app.workers.tasks.calendly_push_task.push_pending_calendly_appointments")
def push_pending_calendly_appointments() -> int:
    return asyncio.run(_push_pending_calendly_appointments())
