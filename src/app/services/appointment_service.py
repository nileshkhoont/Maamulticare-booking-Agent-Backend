from datetime import datetime

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
from app.core.exceptions import NotFoundError, SlotUnavailableError
from app.models.appointment import Appointment
from app.repositories.appointment_repository import appointment_repository
from app.repositories.person_repository import person_repository
from app.services.slot_service import slot_service
from app.utils.datetime_utils import ensure_utc


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

        # Free the old slot first so the new insert's unique-index check doesn't collide with it.
        previous_google_calendar_sync_status = existing.google_calendar_sync_status
        previous_whatsapp_notification_status = existing.whatsapp_notification_status
        new_whatsapp_notification_status = existing.whatsapp_notification_status
        if existing.whatsapp_notification_status in (
            WhatsAppNotificationStatus.pending,
            WhatsAppNotificationStatus.failed,
        ):
            # This row's own notification (most commonly its original "booked" confirmation)
            # never actually reached the patient yet — silently drop it rather than let the push
            # sweep later send a stale confirmation for a slot that no longer exists. Rolled back
            # below if the new insert fails. Already-sent/in-flight statuses are left alone —
            # there's nothing to take back, and the new row's own "rescheduled" message already
            # covers this transition for the patient.
            new_whatsapp_notification_status = None
        # $set-only update (not .save()) — never overwrite fields a concurrent push-task sweep
        # (Google Calendar or WhatsApp) may be mid-write on this same row. See
        # whatsapp_notification_service.py's module docstring for the race this avoids.
        await existing.update(
            Set(
                {
                    # Queues the old row's Google Calendar event deletion — rolled back below
                    # alongside the status flip if the new insert fails, so a failed reschedule
                    # never leaves the original, still-active appointment incorrectly queued to
                    # have its calendar event deleted too.
                    Appointment.status: AppointmentStatus.cancelled,
                    Appointment.google_calendar_sync_status: GoogleCalendarSyncStatus.pending,
                    Appointment.whatsapp_notification_status: new_whatsapp_notification_status,
                }
            )
        )

        try:
            await new_appointment.insert()
        except DuplicateKeyError as exc:
            # Roll back the cancellation so the person keeps their original appointment.
            await existing.update(
                Set(
                    {
                        Appointment.status: AppointmentStatus.rescheduled,
                        Appointment.google_calendar_sync_status: previous_google_calendar_sync_status,
                        Appointment.whatsapp_notification_status: previous_whatsapp_notification_status,
                    }
                )
            )
            raise SlotUnavailableError("Slot was just booked by someone else") from exc

        return new_appointment

    async def cancel(self, appointment_id: str, reason: str | None = None) -> Appointment:
        appointment = await appointment_repository.get_by_id(appointment_id)
        if appointment is None:
            raise NotFoundError("Appointment not found")

        new_notes = f"{appointment.notes or ''}\nCancelled: {reason}".strip() if reason else appointment.notes
        # $set-only update (not .save()) — never overwrite fields a concurrent push-task sweep
        # (Google Calendar or WhatsApp) may be mid-write on this same row.
        await appointment.update(
            Set(
                {
                    Appointment.status: AppointmentStatus.cancelled,
                    Appointment.notes: new_notes,
                    # Queues a Google Calendar event deletion — a no-op in
                    # google_calendar_sync_service if this appointment never actually reached
                    # Google Calendar (event_id still None).
                    Appointment.google_calendar_sync_status: GoogleCalendarSyncStatus.pending,
                    # Queued for the WhatsApp push sweep — "cancelled" message — only while
                    # configured, same reasoning as book_first_time. Only reached via a direct,
                    # standalone cancel (this method) — reschedule_existing's internal old-row
                    # cancellation does NOT call this method, so it never sets these fields (see
                    # that method's comment).
                    Appointment.whatsapp_notification_type: (
                        WhatsAppNotificationType.cancelled if settings.meta_whatsapp_configured else None
                    ),
                    Appointment.whatsapp_notification_status: (
                        WhatsAppNotificationStatus.pending if settings.meta_whatsapp_configured else None
                    ),
                }
            )
        )
        return appointment

    async def get_by_id(self, appointment_id: str) -> Appointment | None:
        return await appointment_repository.get_by_id(appointment_id)

    async def get_active_for_person(self, person_id: str) -> Appointment | None:
        return await appointment_repository.get_active_for_person(person_id)

    async def list_upcoming_for_person(self, person_id: str) -> list[Appointment]:
        return await appointment_repository.list_upcoming_for_person(person_id)


appointment_service = AppointmentService()
