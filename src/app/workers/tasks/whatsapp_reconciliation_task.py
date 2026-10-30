"""Reconciliation sweep for the WhatsApp push queue — un-sticks any Appointment stuck `sending`
longer than a crash could plausibly explain, back to `pending`, and bounded-retries `failed` rows
a limited number of times (NOT forever, unlike Google Calendar's reconciliation sweep) — a
WhatsApp send failure is far more often permanent (invalid/unreachable number, rejected template,
outside messaging policy) than Google Calendar's typically-transient config errors, so blindly
resurrecting `failed` rows forever would just re-attempt a hopeless send on every tick.

There is no inbound-catchup pass at all, unlike a two-way sync — the only inbound traffic this
integration has is Meta's own status webhook (handled directly in api/v1/endpoints/webhooks.py as
it arrives), never anything this sweep needs to pull.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from beanie.operators import Set

from app.core.constants import WhatsAppNotificationStatus
from app.core.logging import get_logger
from app.db.mongodb import close_db, connect_db
from app.models.appointment import Appointment
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

STUCK_SENDING_THRESHOLD_MINUTES = 15
MAX_WHATSAPP_SEND_ATTEMPTS = 5


async def reconcile_whatsapp_notifications_once() -> int:
    """Core sweep logic — assumes the DB is already connected. Shared by the standalone Celery
    task below and by workers/inprocess_scheduler.py.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=STUCK_SENDING_THRESHOLD_MINUTES)
    retryable_failed = await Appointment.find(
        Appointment.whatsapp_notification_status == WhatsAppNotificationStatus.failed,
        Appointment.whatsapp_notification_attempt_count < MAX_WHATSAPP_SEND_ATTEMPTS,
        Appointment.is_deleted == False,  # noqa: E712
    ).to_list()
    stuck_sending = await Appointment.find(
        Appointment.whatsapp_notification_status == WhatsAppNotificationStatus.sending,
        Appointment.updated_at < cutoff,
        Appointment.is_deleted == False,  # noqa: E712
    ).to_list()
    for appointment in [*retryable_failed, *stuck_sending]:
        # $set-only update (not .save()) — never overwrite fields another concurrent sweep
        # (e.g. google_calendar_push_task) may be mid-write on this same row. See
        # whatsapp_notification_service.py's module docstring for the race this avoids.
        await appointment.update(Set({Appointment.whatsapp_notification_status: WhatsAppNotificationStatus.pending}))
        logger.info("whatsapp_notification_unstuck", appointment_id=str(appointment.id))
    return len(retryable_failed) + len(stuck_sending)


async def _reconcile_whatsapp_notifications() -> int:
    """Standalone entry point for the Celery task — owns its own DB connection since it runs in
    a separate worker process from the API.
    """
    await connect_db()
    try:
        return await reconcile_whatsapp_notifications_once()
    finally:
        await close_db()


@celery_app.task(name="app.workers.tasks.whatsapp_reconciliation_task.reconcile_whatsapp_notifications")
def reconcile_whatsapp_notifications() -> int:
    return asyncio.run(_reconcile_whatsapp_notifications())
