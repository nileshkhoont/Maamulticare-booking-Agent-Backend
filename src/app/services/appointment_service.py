from datetime import datetime

from beanie import UpdateResponse
from beanie.operators import Set
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.constants import (
    AppointmentStatus,
    BookingSource,
    GoogleCalendarSyncStatus,
    WhatsAppNotificationStatus,
    WhatsAppNotificationType,
)
from app.core.exceptions import AppointmentNotActiveError, NotFoundError, SlotUnavailableError
from app.db.base import utcnow
from app.models.appointment import Appointment
from app.repositories.appointment_repository import ACTIVE_STATUSES as _ACTIVE_STATUSES
from app.repositories.appointment_repository import appointment_repository
from app.repositories.person_repository import person_repository
from app.services.slot_service import slot_service
from app.utils.datetime_utils import ensure_utc


def _not_active_message(status: AppointmentStatus) -> str:
    if status == AppointmentStatus.cancelled:
        return "This appointment is already cancelled"
    return f"This appointment is {status.value.replace('_', ' ')} and can no longer be cancelled"


class AppointmentService:
    """Booking, rescheduling, conflict checks (folder-structure doc). No follow-up-appointment
    code path — confirmed scope is first-time booking + reschedule of an existing appointment
    only, and booking is not doctor-wise.
    """

    async def book_first_time(
        self,
        person_id: str,
        appointment_datetime: datetime,
        booking_source: BookingSource,
        duration_minutes: int | None = None,
        notes: str | None = None,
        created_by_call_id: str | None = None,
        pending_edesy_call_id: str | None = None,
    ) -> Appointment:
        if await person_repository.get_by_id(person_id) is None:
            raise NotFoundError("Person not found")

        appointment_datetime = ensure_utc(appointment_datetime)
        slot = await slot_service.check_availability(appointment_datetime)
        if not slot.available:
            raise SlotUnavailableError(slot.reason)

        appointment = Appointment(
            person_id=person_id,
            appointment_datetime=appointment_datetime,
            duration_minutes=duration_minutes,
            status=AppointmentStatus.booked,
            booking_source=booking_source,
            notes=notes,
            created_by_call_id=created_by_call_id,
            pending_edesy_call_id=pending_edesy_call_id,
            # Queued for workers/tasks/google_calendar_push_task.py's sweep to push to Google Calendar.
            google_calendar_sync_status=GoogleCalendarSyncStatus.pending,
            # Queued for workers/tasks/whatsapp_push_task.py's sweep to WhatsApp the patient — but
            # only while WhatsApp is actually configured (see Settings.meta_whatsapp_configured).
            # Left None while it's not, rather than queued as pending, so no backlog accumulates
            # during an unconfigured window for a later sweep to blast out all at once, stale, the
            # moment it does get configured.
            whatsapp_notification_type=(
                WhatsAppNotificationType.booked if settings.meta_whatsapp_configured else None
            ),
            whatsapp_notification_status=(
                WhatsAppNotificationStatus.pending if settings.meta_whatsapp_configured else None
            ),
        )
        try:
            await appointment.insert()
        except DuplicateKeyError as exc:
            raise SlotUnavailableError("Slot was just booked by someone else") from exc
        return appointment

    async def reschedule_existing(
        self,
        appointment_id: str,
        new_appointment_datetime: datetime,
        notes: str | None = None,
        created_by_call_id: str | None = None,
        pending_edesy_call_id: str | None = None,
    ) -> Appointment:
        existing = await appointment_repository.get_by_id(appointment_id)
        if existing is None or existing.status not in (
            AppointmentStatus.booked,
            AppointmentStatus.rescheduled,
        ):
            raise NotFoundError("No active appointment found to reschedule")

        new_appointment_datetime = ensure_utc(new_appointment_datetime)
        if new_appointment_datetime == existing.appointment_datetime:
            # The requested slot is the one this appointment already holds. Same refusal (and the
            # same message) the slot check below has always given for this case — made explicit
            # so the insert-new-first order further down can never see a same-slot collision.
            raise SlotUnavailableError("Slot is already booked")
        slot = await slot_service.check_availability(
            new_appointment_datetime, exclude_appointment_id=appointment_id
        )
        if not slot.available:
            raise SlotUnavailableError(slot.reason)

        new_appointment = Appointment(
            person_id=existing.person_id,
            appointment_datetime=new_appointment_datetime,
            duration_minutes=existing.duration_minutes,
            status=AppointmentStatus.rescheduled,
            booking_source=existing.booking_source,
            original_appointment_id=str(existing.id),
            notes=notes or existing.notes,
            created_by_call_id=created_by_call_id,
            pending_edesy_call_id=pending_edesy_call_id,
            # Queued for the push sweep like book_first_time.
            google_calendar_sync_status=GoogleCalendarSyncStatus.pending,
            # Queued for the WhatsApp push sweep — "rescheduled" message, new date/time — only
            # while configured, same reasoning as book_first_time above. The OLD row below never
            # gets a NEW WhatsApp notification queued (no "cancelled" message for it), so the
            # patient gets exactly one message, not also a confusing second one — see
            # services/whatsapp_notification_service.py's module docstring.
            whatsapp_notification_type=(
                WhatsAppNotificationType.rescheduled if settings.meta_whatsapp_configured else None
            ),
            whatsapp_notification_status=(
                WhatsAppNotificationStatus.pending if settings.meta_whatsapp_configured else None
            ),
        )

        # Insert the NEW row first, and only then retire the old one. The new time always differs
        # from the old row's (same-time requests are refused above), so the one-active-booking-
        # per-slot unique index can never collide with the old row. If the insert fails because
        # someone just took the new slot, nothing has been changed yet: the original appointment
        # stays exactly as it was (status, calendar, WhatsApp). The previous order — cancel old,
        # insert new, roll back on failure — restored the wrong status ("rescheduled" even for a
        # "booked" original) and left the old slot briefly free for someone else to take.
        try:
            await new_appointment.insert()
        except DuplicateKeyError as exc:
            raise SlotUnavailableError("Slot was just booked by someone else") from exc

        new_whatsapp_notification_status = existing.whatsapp_notification_status
        if existing.whatsapp_notification_status in (
            WhatsAppNotificationStatus.pending,
            WhatsAppNotificationStatus.failed,
        ):
            # This row's own notification (most commonly its original "booked" confirmation)
            # never actually reached the patient yet — silently drop it rather than let the push
            # sweep later send a stale confirmation for a slot that no longer exists.
            # Already-sent/in-flight statuses are left alone — there's nothing to take back, and
            # the new row's own "rescheduled" message already covers this transition.
            new_whatsapp_notification_status = None
        # Retire the old row — conditionally, as one atomic write: only if it is STILL active.
        # $set-only (not .save()), so a concurrent push-task sweep's fields on this row are never
        # overwritten (see whatsapp_notification_service.py's module docstring).
        retired = await Appointment.find_one(
            Appointment.id == existing.id,
            {"status": {"$in": [s.value for s in _ACTIVE_STATUSES]}},
        ).update(
            Set(
                {
                    Appointment.status: AppointmentStatus.cancelled,
                    # Queues the old row's Google Calendar event deletion.
                    Appointment.google_calendar_sync_status: GoogleCalendarSyncStatus.pending,
                    Appointment.whatsapp_notification_status: new_whatsapp_notification_status,
                }
            )
        )
        if retired.modified_count == 0:
            # Another request changed this appointment between our read and now — a second
            # reschedule that won the race, or a cancel. Without this check both reschedules
            # "succeeded" and the patient ended up with two appointments (and two "rescheduled"
            # messages); a reschedule racing a cancel resurrected an appointment the patient had
            # just cancelled. Discard the row we just inserted, before any sweep acts on it, and
            # answer exactly as for any appointment that is no longer active.
            await self._discard_unused_reschedule(new_appointment)
            raise NotFoundError("No active appointment found to reschedule")

        return new_appointment

    async def _discard_unused_reschedule(self, appointment: Appointment) -> None:
        """Hides a just-inserted reschedule row that lost a race, and frees its slot. It never
        reached the patient or the calendar, so nothing is queued for it: no WhatsApp, and no
        Google Calendar work (the push/reconciliation sweeps only ever look at non-deleted rows).
        """
        await appointment.update(
            Set(
                {
                    Appointment.status: AppointmentStatus.cancelled,
                    Appointment.is_deleted: True,
                    Appointment.deleted_at: utcnow(),
                    Appointment.google_calendar_sync_status: GoogleCalendarSyncStatus.not_applicable,
                    Appointment.whatsapp_notification_type: None,
                    Appointment.whatsapp_notification_status: None,
                }
            )
        )

    async def cancel(self, appointment_id: str, reason: str | None = None) -> Appointment:
        appointment = await appointment_repository.get_by_id(appointment_id)
        if appointment is None:
            raise NotFoundError("Appointment not found")
        if appointment.status not in _ACTIVE_STATUSES:
            # Cancelling again would only duplicate the notes/audit trail and re-queue
            # notifications; cancelling a completed/no-show one would overwrite its history.
            raise AppointmentNotActiveError(_not_active_message(appointment.status))

        new_notes = f"{appointment.notes or ''}\nCancelled: {reason}".strip() if reason else appointment.notes
        updates = {
            Appointment.status: AppointmentStatus.cancelled,
            Appointment.notes: new_notes,
            # Queues a Google Calendar event deletion — a no-op in google_calendar_sync_service if
            # this appointment never actually reached Google Calendar (event_id still None).
            Appointment.google_calendar_sync_status: GoogleCalendarSyncStatus.pending,
            # Queued for the WhatsApp push sweep — "cancelled" message — only while configured,
            # same reasoning as book_first_time. Only reached via a direct, standalone cancel
            # (this method) — reschedule_existing's internal old-row cancellation does NOT call
            # this method, so it never sets these fields (see that method's comment).
            Appointment.whatsapp_notification_type: (
                WhatsAppNotificationType.cancelled if settings.meta_whatsapp_configured else None
            ),
            Appointment.whatsapp_notification_status: (
                WhatsAppNotificationStatus.pending if settings.meta_whatsapp_configured else None
            ),
        }
        if settings.meta_whatsapp_configured:
            # A NEW message is being queued on this row, so the previous message's send state is
            # cleared. Without this, the "booked" confirmation's message id left on the row made
            # the push sweep treat the cancellation as already sent — so a patient whose booking
            # had been confirmed never received the cancellation at all.
            updates.update(
                {
                    Appointment.whatsapp_message_id: None,
                    Appointment.whatsapp_notification_error: None,
                    Appointment.whatsapp_notification_attempt_count: 0,
                    Appointment.whatsapp_notification_last_event_at: None,
                    Appointment.whatsapp_send_started_at: None,
                }
            )

        # Conditional on the row still being active, as one atomic write — of two simultaneous
        # cancels, exactly one wins. $set-only (not .save()), so a concurrent push-task sweep's
        # fields on this row are never overwritten.
        cancelled = await Appointment.find_one(
            Appointment.id == appointment.id,
            {"status": {"$in": [s.value for s in _ACTIVE_STATUSES]}},
        ).update(Set(updates), response_type=UpdateResponse.NEW_DOCUMENT)
        if cancelled is None:
            raise AppointmentNotActiveError(_not_active_message(AppointmentStatus.cancelled))
        return cancelled

    async def get_by_id(self, appointment_id: str) -> Appointment | None:
        return await appointment_repository.get_by_id(appointment_id)

    async def get_active_for_person(self, person_id: str) -> Appointment | None:
        return await appointment_repository.get_active_for_person(person_id)

    async def list_upcoming_for_person(self, person_id: str) -> list[Appointment]:
        return await appointment_repository.list_upcoming_for_person(person_id)


appointment_service = AppointmentService()
