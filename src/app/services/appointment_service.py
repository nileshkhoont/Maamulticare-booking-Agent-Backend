from datetime import datetime

from pymongo.errors import DuplicateKeyError

from app.core.constants import AppointmentStatus, BookingSource
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
        )

        # Free the old slot first so the new insert's unique-index check doesn't collide with it.
        existing.status = AppointmentStatus.cancelled
        await existing.save()

        try:
            await new_appointment.insert()
        except DuplicateKeyError as exc:
            # Roll back the cancellation so the person keeps their original appointment.
            existing.status = AppointmentStatus.rescheduled
            await existing.save()
            raise SlotUnavailableError("Slot was just booked by someone else") from exc

        return new_appointment

    async def cancel(self, appointment_id: str, reason: str | None = None) -> Appointment:
        appointment = await appointment_repository.get_by_id(appointment_id)
        if appointment is None:
            raise NotFoundError("Appointment not found")

        appointment.status = AppointmentStatus.cancelled
        if reason:
            appointment.notes = f"{appointment.notes or ''}\nCancelled: {reason}".strip()
        await appointment.save()
        return appointment

    async def get_by_id(self, appointment_id: str) -> Appointment | None:
        return await appointment_repository.get_by_id(appointment_id)

    async def get_active_for_person(self, person_id: str) -> Appointment | None:
        return await appointment_repository.get_active_for_person(person_id)

    async def list_upcoming_for_person(self, person_id: str) -> list[Appointment]:
        return await appointment_repository.list_upcoming_for_person(person_id)


appointment_service = AppointmentService()
