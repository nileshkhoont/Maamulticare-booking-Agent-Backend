"""Pushes Appointments queued with whatsapp_notification_status=pending out as WhatsApp template
messages — the (only) outbound half of the one-way WhatsApp notification channel (see
services/whatsapp_notification_service.py). Poll-based, not fired inline from
appointment_service.py's book/reschedule/cancel, matching this codebase's existing convention of
never calling an external integration synchronously from an admin-facing request.
"""

import asyncio

from app.core.config import settings
from app.core.logging import get_logger
from app.db.mongodb import close_db, connect_db
from app.repositories.appointment_repository import appointment_repository
from app.services.whatsapp_notification_service import whatsapp_notification_service
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def push_pending_whatsapp_notifications_once() -> int:
    """Core sweep logic — assumes the DB is already connected. Shared by the standalone Celery
    task below and by workers/inprocess_scheduler.py.
    """
    if not settings.meta_whatsapp_notifications_enabled:
        logger.warning("whatsapp_push_skipped", reason="META_WHATSAPP_NOTIFICATIONS_ENABLED is false")
        return 0
    if not settings.meta_whatsapp_access_token or not settings.meta_whatsapp_phone_number_id:
        logger.warning("whatsapp_push_skipped", reason="META_WHATSAPP_ACCESS_TOKEN/PHONE_NUMBER_ID not configured")
        return 0

    pending = await appointment_repository.list_pending_whatsapp_notifications()
    for appointment in pending:
        try:
            await whatsapp_notification_service.process_pending_appointment(appointment)
        except Exception:
            # process_pending_appointment already catches WhatsAppIntegrationError internally;
            # this outer guard is belt-and-suspenders against a genuinely unexpected bug on one
            # appointment taking down the whole sweep tick for every other pending row.
            logger.exception("whatsapp_push_unexpected_error", appointment_id=str(appointment.id))
    return len(pending)


async def _push_pending_whatsapp_notifications() -> int:
    """Standalone entry point for the Celery task — owns its own DB connection since it runs in
    a separate worker process from the API.
    """
    await connect_db()
    try:
        return await push_pending_whatsapp_notifications_once()
    finally:
        await close_db()


@celery_app.task(name="app.workers.tasks.whatsapp_push_task.push_pending_whatsapp_notifications")
def push_pending_whatsapp_notifications() -> int:
    return asyncio.run(_push_pending_whatsapp_notifications())
