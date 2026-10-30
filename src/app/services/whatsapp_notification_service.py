"""One-way, OUTBOUND-ONLY WhatsApp notifications: sends a pre-approved template message to the
patient whenever appointment_service.py books/reschedules/cancels their appointment. Patients can
NEVER act on an appointment through WhatsApp — only via the Edesy AI phone system. Any reply they
send lands in the clinic's own WhatsApp Business app for a human to read; this system never
processes it (see api/v1/endpoints/webhooks.py's POST /webhooks/whatsapp, which logs and ignores
`messages[]` payload entries by design).

Outbound sends are poll-based (workers/tasks/whatsapp_push_task.py), not fired inline from
appointment_service.py, matching this codebase's existing convention of never calling an external
integration synchronously from an admin-facing request.

Unlike Google Calendar, reschedule needs ONE piece of special handling: appointment_service.
reschedule_existing creates a brand-new Appointment row (status=rescheduled) and independently
cancels the old one (status=cancelled) as two separate rows — for Google Calendar that's fine
(delete old event, create new event), but for a patient-facing message, notifying on BOTH rows
would send a confusing "cancelled" message immediately followed by a "rescheduled" one for what is
really one action. Fix lives in appointment_service.py: only the new (rescheduled) row ever gets
whatsapp_notification_status=pending set — the old (now-cancelled) row's WhatsApp fields are left
None entirely, so the push sweep (which only ever queries status=pending) never picks it up.

Every write here uses Beanie's atomic `Set` operator (a MongoDB `$set` touching only the named
fields) instead of `appointment.save()`. `save()` writes EVERY field from this process's in-memory
copy of the document — including whatsapp_notification_status/whatsapp_message_id, which this
exact appointment's google_calendar_sync_service is independently reading/writing on its OWN
in-memory copy of the SAME row at roughly the same time (both are poll-based sweeps that pick up a
freshly-booked appointment within the same ~30s window). Two `save()` calls racing like that is a
classic lost-update: whichever one saves last silently overwrites the other's fields back to its
own stale snapshot — root-caused 2026-10-15 from a real double-send (whatsapp_notification_status
got reverted from "sent" back to "pending" by a concurrent google_calendar_push_task save, so the
next whatsapp sweep tick saw "pending" again and sent a second message). `Set` sidesteps this
entirely: it only ever touches the fields named in this file, so it can never clobber a field some
other concurrent writer owns.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from beanie.operators import Set

from app.core.config import settings
from app.core.constants import WhatsAppNotificationStatus, WhatsAppNotificationType
from app.core.exceptions import WhatsAppIntegrationError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.integrations.whatsapp.client import whatsapp_client
from app.models.appointment import Appointment
from app.models.business_config import BusinessConfig
from app.repositories.person_repository import person_repository
from app.utils.datetime_utils import ensure_utc

logger = get_logger(__name__)

_DEFAULT_TIMEZONE = "Asia/Kolkata"  # fallback only — see _resolve_timezone

_TEMPLATE_NAMES: dict[WhatsAppNotificationType, str] = {
    WhatsAppNotificationType.booked: "appointment_booked_v1",
    WhatsAppNotificationType.rescheduled: "appointment_rescheduled_v1",
    WhatsAppNotificationType.cancelled: "appointment_cancelled_v1",
}

# Status rank used to decide whether a status webhook event is forward progress (ignore a
# same-or-lower-rank redelivery) or must be applied (a higher rank, or "failed" regardless of
# rank — see process_status_webhook_event).
_STATUS_RANK: dict[str, int] = {
    WhatsAppNotificationStatus.pending.value: 0,
    WhatsAppNotificationStatus.sending.value: 1,
    WhatsAppNotificationStatus.sent.value: 2,
    WhatsAppNotificationStatus.delivered.value: 3,
    WhatsAppNotificationStatus.read.value: 4,
}


class WhatsAppNotificationService:
    async def _resolve_timezone(self) -> str:
        config = await BusinessConfig.find_one({})
        return config.timezone if config and config.timezone else _DEFAULT_TIMEZONE

    def _format_phone(self, phone_number: str) -> str:
        # Meta's `to` field wants digits only, no leading "+" (Person.phone_number is stored
        # E.164, e.g. "+919876543210").
        return phone_number.lstrip("+")

    def _format_datetime(self, dt_utc: datetime, timezone: str) -> str:
        try:
            localized = dt_utc.astimezone(ZoneInfo(timezone))
        except ZoneInfoNotFoundError:
            logger.warning("whatsapp_invalid_timezone", timezone=timezone)
            localized = dt_utc.astimezone(ZoneInfo(_DEFAULT_TIMEZONE))
        return localized.strftime("%d %b %Y, %I:%M %p")

    # --- Outbound: app -> WhatsApp (called only by whatsapp_push_task's sweep) ---

    async def process_pending_appointment(self, appointment: Appointment) -> None:
        if appointment.whatsapp_message_id is not None:
            # Already sent on an earlier tick (e.g. this row was re-queued) — don't double-send.
            await appointment.update(Set({Appointment.whatsapp_notification_status: WhatsAppNotificationStatus.sent}))
            return

        try:
            person = await person_repository.get_by_id(appointment.person_id)
            if person is None:
                raise WhatsAppIntegrationError(
                    f"Person {appointment.person_id} not found for WhatsApp notification"
                )

            template_name = _TEMPLATE_NAMES[appointment.whatsapp_notification_type]
            timezone = await self._resolve_timezone()
            formatted_datetime = self._format_datetime(
                ensure_utc(appointment.appointment_datetime), timezone
            )

            await appointment.update(
                Set(
                    {
                        Appointment.whatsapp_notification_attempt_count: appointment.whatsapp_notification_attempt_count
                        + 1,
                        Appointment.whatsapp_notification_status: WhatsAppNotificationStatus.sending,
                    }
                )
            )

            result = await whatsapp_client.send_template_message(
                to=self._format_phone(person.phone_number),
                template_name=template_name,
                language_code=settings.meta_whatsapp_template_language_code,
                body_parameters=[person.full_name or person.phone_number, formatted_datetime],
            )

            await appointment.update(
                Set(
                    {
                        Appointment.whatsapp_message_id: result.message_id,
                        Appointment.whatsapp_notification_status: WhatsAppNotificationStatus.sent,
                        Appointment.whatsapp_notification_error: None,
                        Appointment.whatsapp_notification_last_event_at: utcnow(),
                    }
                )
            )
            logger.info(
                "whatsapp_notification_sent",
                appointment_id=str(appointment.id),
                message_id=result.message_id,
            )
        except WhatsAppIntegrationError as exc:
            await appointment.update(
                Set(
                    {
                        Appointment.whatsapp_notification_status: WhatsAppNotificationStatus.failed,
                        Appointment.whatsapp_notification_error: str(exc),
                    }
                )
            )
            logger.error("whatsapp_notification_failed", appointment_id=str(appointment.id), error=str(exc))

    # --- Inbound (status only): Meta's status webhook -> app ---

    async def process_status_webhook_event(
        self, message_id: str, new_status: str, error_detail: str | None
    ) -> None:
        appointment = await Appointment.find_one(Appointment.whatsapp_message_id == message_id)
        if appointment is None:
            # Not one of ours (e.g. a message sent by hand from the WhatsApp Business app
            # directly) — nothing to update, never an error.
            logger.info("whatsapp_status_event_unmatched", message_id=message_id, status=new_status)
            return

        updates: dict = {Appointment.whatsapp_notification_last_event_at: utcnow()}
        if new_status == WhatsAppNotificationStatus.failed.value:
            updates[Appointment.whatsapp_notification_status] = WhatsAppNotificationStatus.failed
            updates[Appointment.whatsapp_notification_error] = (
                error_detail or "WhatsApp reported delivery failure"
            )
        else:
            current_rank = _STATUS_RANK.get(
                appointment.whatsapp_notification_status.value
                if appointment.whatsapp_notification_status
                else "",
                -1,
            )
            new_rank = _STATUS_RANK.get(new_status, -1)
            if new_rank <= current_rank:
                # Same-or-lower-rank redelivery (Meta can redeliver/reorder status webhooks) —
                # a pure no-op, not an error.
                return
            updates[Appointment.whatsapp_notification_status] = WhatsAppNotificationStatus(new_status)

        await appointment.update(Set(updates))
        logger.info(
            "whatsapp_status_event_applied",
            appointment_id=str(appointment.id),
            message_id=message_id,
            status=new_status,
        )


whatsapp_notification_service = WhatsAppNotificationService()
