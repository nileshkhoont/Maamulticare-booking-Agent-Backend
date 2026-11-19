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
    # "Stuck" is measured from when the send actually started (whatsapp_send_started_at), never
    # from updated_at — updated_at is only set at creation, so any appointment older than the
    # threshold used to look stuck the instant a send began, and got sent a second time while
    # the first was still in flight. Rows already sending at deploy time have no start stamp, so
    # only those fall back to the old updated_at comparison.
    stuck_sending = await Appointment.find(
        Appointment.whatsapp_notification_status == WhatsAppNotificationStatus.sending,
        Appointment.is_deleted == False,  # noqa: E712
        {
            "$or": [
                {"whatsapp_send_started_at": {"$lt": cutoff}},
                {"whatsapp_send_started_at": None, "updated_at": {"$lt": cutoff}},
            ]
        },
    ).to_list()

    reset = 0
    for appointment in retryable_failed:
        reset += await _reset_to_pending(
            appointment, Appointment.whatsapp_notification_status == WhatsAppNotificationStatus.failed
        )
    for appointment in stuck_sending:
        reset += await _reset_to_pending(
            appointment,
            Appointment.whatsapp_notification_status == WhatsAppNotificationStatus.sending,
            Appointment.whatsapp_send_started_at == appointment.whatsapp_send_started_at,
        )
    return reset


async def _reset_to_pending(appointment: Appointment, *still_in_state) -> int:
    """Conditional $set: only resets the row if it is STILL in the state this sweep read it in, so a
    send that finished (or was re-claimed) after the read is never knocked back to pending.
    """
    result = await Appointment.find_one(Appointment.id == appointment.id, *still_in_state).update(
        Set({Appointment.whatsapp_notification_status: WhatsAppNotificationStatus.pending})
    )
    if result.modified_count == 0:
        return 0
    logger.info("whatsapp_notification_unstuck", appointment_id=str(appointment.id))
    return 1


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
