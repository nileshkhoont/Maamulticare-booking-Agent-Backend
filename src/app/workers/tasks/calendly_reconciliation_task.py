"""Reconciliation sweep for the Calendly sync — the production-safe mitigation for two-way sync
while the account lacks real-time webhooks (currently on Calendly's free plan; webhooks require a
paid Standard+ plan, confirmed live against Calendly's docs 2026-10-08), and a useful safety net
afterward too, since webhook delivery is never 100% guaranteed. Two independent, fail-soft passes:

  1. Un-stick any Appointment whose last Calendly push attempt failed, or has been `syncing` for
     longer than a crash could plausibly explain.
  2. Inbound catch-up — pulls Calendly's own scheduled events directly and runs each invitee
     through the exact same loop-prevention + create/cancel handling the webhook route uses
     (calendly_sync_service.handle_webhook_event), so a doctor-sent invite link or a patient's
     self-service reschedule/cancel still reaches our system even with zero webhook deliveries.
     Safe to re-run on overlapping windows — every path it calls is already idempotent.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.constants import CalendlySyncStatus
from app.core.logging import get_logger
from app.db.mongodb import close_db, connect_db
from app.integrations.calendly.client import calendly_client
from app.integrations.calendly.webhook_events import CalendlyWebhookEvent, CalendlyWebhookInviteePayload
from app.models.appointment import Appointment
from app.models.business_config import BusinessConfig
from app.services.calendly_sync_service import calendly_sync_service
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

STUCK_SYNCING_THRESHOLD_MINUTES = 15
DEFAULT_RECONCILE_WINDOW_DAYS = 30


async def _unstick_failed_or_stuck() -> int:
    cutoff = datetime.now(UTC) - timedelta(minutes=STUCK_SYNCING_THRESHOLD_MINUTES)
    failed = await Appointment.find(
        Appointment.calendly_sync_status == CalendlySyncStatus.failed,
        Appointment.is_deleted == False,  # noqa: E712
    ).to_list()
    stuck_syncing = await Appointment.find(
        Appointment.calendly_sync_status == CalendlySyncStatus.syncing,
        Appointment.updated_at < cutoff,
        Appointment.is_deleted == False,  # noqa: E712
    ).to_list()
    for appointment in [*failed, *stuck_syncing]:
        appointment.calendly_sync_status = CalendlySyncStatus.pending
        await appointment.save()
        logger.info("calendly_sync_unstuck", appointment_id=str(appointment.id))
    return len(failed) + len(stuck_syncing)


async def _inbound_catch_up() -> int:
    try:
        user = await calendly_client.get_current_user()
    except Exception:
        logger.exception("calendly_reconciliation_get_user_failed")
        return 0

    config = await BusinessConfig.find_one({})
    window_days = (config.max_advance_booking_days if config else None) or DEFAULT_RECONCILE_WINDOW_DAYS
    now = datetime.now(UTC)
    min_start = now.isoformat()
    max_start = (now + timedelta(days=window_days)).isoformat()

    events = []
    for status in ("active", "canceled"):
        try:
            events.extend(
                await calendly_client.list_scheduled_events(
                    user=user.resource.uri, min_start_time=min_start, max_start_time=max_start, status=status
                )
            )
        except Exception:
            logger.exception("calendly_reconciliation_list_events_failed", status=status)

    # Calendly's GET /scheduled_events has no event_type filter param (confirmed against its own
    # OpenAPI spec) — it returns every event for the host across ALL of their Event Types. This
    # account has more than one (e.g. a separate "OPD Consultation" type unrelated to this
    # integration), so without this filter their bookings would get synced into our Appointments
    # collection too. Filter client-side to only the one Event Type this integration owns —
    # settings.calendly_event_type_uri, read from CALENDLY_EVENT_TYPE_URI in .env, never hardcoded.
    events = [e for e in events if e.event_type == settings.calendly_event_type_uri]

    processed = 0
    for scheduled_event in events:
        event_uuid = scheduled_event.uri.rstrip("/").rsplit("/", 1)[-1]
        try:
            invitees = await calendly_client.get_event_invitees(event_uuid)
        except Exception:
            logger.exception("calendly_reconciliation_get_invitees_failed", event_uuid=event_uuid)
            continue

        for invitee in invitees:
            payload = CalendlyWebhookInviteePayload(
                uri=invitee.uri,
                event=invitee.event,
                email=invitee.email,
                name=invitee.name,
                status=invitee.status,
                timezone=invitee.timezone,
                text_reminder_number=invitee.text_reminder_number,
                questions_and_answers=invitee.questions_and_answers,
                rescheduled=invitee.rescheduled,
                old_invitee=invitee.old_invitee,
                new_invitee=invitee.new_invitee,
                cancellation=invitee.cancellation,
            )
            synthetic_event = CalendlyWebhookEvent(
                event="invitee.canceled" if invitee.status == "canceled" else "invitee.created",
                payload=payload,
            )
            await calendly_sync_service.handle_webhook_event(synthetic_event)
            processed += 1

    return processed


async def reconcile_calendly_once() -> int:
    """Core sweep logic — assumes the DB is already connected. Shared by the standalone Celery
    task below and by workers/inprocess_scheduler.py.
    """
    unstuck = await _unstick_failed_or_stuck()
    processed = await _inbound_catch_up()
    return unstuck + processed


async def _reconcile_calendly() -> int:
    await connect_db()
    try:
        return await reconcile_calendly_once()
    finally:
        await close_db()


@celery_app.task(name="app.workers.tasks.calendly_reconciliation_task.reconcile_calendly")
def reconcile_calendly() -> int:
    return asyncio.run(_reconcile_calendly())
