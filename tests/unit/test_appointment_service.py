from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import AppointmentStatus, BookingSource
from app.core.exceptions import SlotUnavailableError
from app.models.business_config import BusinessConfig
from app.models.person import Person
from app.services.appointment_service import appointment_service


def _next_monday_9am() -> datetime:
    now = datetime.now(UTC)
    days_ahead = (0 - now.weekday()) % 7 or 7
    return (now + timedelta(days=days_ahead)).replace(hour=9, minute=0, second=0, microsecond=0)


async def _make_person(phone_number: str) -> str:
    person = Person(phone_number=phone_number)
    await person.insert()
    return str(person.id)


@pytest.mark.asyncio
async def test_book_first_time_success(business_config: BusinessConfig):
    slot = _next_monday_9am()
    person_id = await _make_person("+15550000001")
    appointment = await appointment_service.book_first_time(
        person_id=person_id, appointment_datetime=slot, booking_source=BookingSource.inbound_call
    )
    assert appointment.status == AppointmentStatus.booked
    assert appointment.person_id == person_id
    # Every new row is queued for the Google Calendar push sweep — see
    # google_calendar_sync_service.py.
    assert appointment.google_calendar_sync_status == "pending"


@pytest.mark.asyncio
async def test_double_booking_same_slot_rejected(business_config: BusinessConfig):
    slot = _next_monday_9am()
    person1_id = await _make_person("+15550000002")
    person2_id = await _make_person("+15550000003")
    await appointment_service.book_first_time(
        person_id=person1_id, appointment_datetime=slot, booking_source=BookingSource.inbound_call
    )

    with pytest.raises(SlotUnavailableError):
        await appointment_service.book_first_time(
            person_id=person2_id, appointment_datetime=slot, booking_source=BookingSource.inbound_call
        )


@pytest.mark.asyncio
async def test_reschedule_frees_original_slot(business_config: BusinessConfig):
    original_slot = _next_monday_9am()
    new_slot = original_slot + timedelta(minutes=30)
    person1_id = await _make_person("+15550000004")
    person2_id = await _make_person("+15550000005")

    original = await appointment_service.book_first_time(
        person_id=person1_id, appointment_datetime=original_slot, booking_source=BookingSource.inbound_call
    )
    rescheduled = await appointment_service.reschedule_existing(
        appointment_id=str(original.id), new_appointment_datetime=new_slot
    )

    assert rescheduled.status == AppointmentStatus.rescheduled
    assert rescheduled.original_appointment_id == str(original.id)
    assert rescheduled.google_calendar_sync_status == "pending"

    # The original slot should be free again — someone else can now book it.
    reclaimed = await appointment_service.book_first_time(
        person_id=person2_id, appointment_datetime=original_slot, booking_source=BookingSource.inbound_call
    )
    assert reclaimed.status == AppointmentStatus.booked


@pytest.mark.asyncio
async def test_reschedule_queues_old_row_for_google_calendar_deletion(business_config: BusinessConfig):
    original_slot = _next_monday_9am()
    new_slot = original_slot + timedelta(minutes=30)
    person_id = await _make_person("+15550000006")

    original = await appointment_service.book_first_time(
        person_id=person_id, appointment_datetime=original_slot, booking_source=BookingSource.inbound_call
    )
    await appointment_service.reschedule_existing(
        appointment_id=str(original.id), new_appointment_datetime=new_slot
    )

    from app.models.appointment import Appointment

    old_refreshed = await Appointment.get(original.id)
    assert old_refreshed.status == AppointmentStatus.cancelled
    assert old_refreshed.google_calendar_sync_status == "pending"


@pytest.mark.asyncio
async def test_reschedule_rolls_back_old_rows_sync_status_on_slot_conflict(business_config: BusinessConfig):
    """If the new insert hits the DB-level unique-index conflict — a genuine race where two
    requests both pass the availability check before either has inserted — the old row's
    google_calendar_sync_status must roll back to its PRE-ATTEMPT value, not just reset to some
    default, otherwise a failed reschedule of an already-synced appointment would leave it
    incorrectly queued to have its Google Calendar event deleted too, even though it's still
    fully active. Forces exactly that race by mocking the upfront availability check to say
    "available" even though slot_b is already taken — the real unique index is what actually
    rejects the insert, same as it would for two truly concurrent requests.
    """
    from unittest.mock import AsyncMock, patch

    from app.models.appointment import Appointment
    from app.schemas.appointment import SlotCheckResponse

    slot_a = _next_monday_9am()
    slot_b = slot_a + timedelta(minutes=30)
    person1_id = await _make_person("+15550000007")
    person2_id = await _make_person("+15550000008")

    original = await appointment_service.book_first_time(
        person_id=person1_id, appointment_datetime=slot_a, booking_source=BookingSource.inbound_call
    )
    # Simulate the push sweep having already run and successfully synced this appointment.
    original.google_calendar_sync_status = "synced"
    original.google_calendar_event_id = str(original.id)
    await original.save()

    # Someone else already holds slot_b.
    await appointment_service.book_first_time(
        person_id=person2_id, appointment_datetime=slot_b, booking_source=BookingSource.inbound_call
    )

    fake_available = AsyncMock(return_value=SlotCheckResponse(available=True))
    with patch("app.services.appointment_service.slot_service.check_availability", fake_available):
        with pytest.raises(SlotUnavailableError):
            await appointment_service.reschedule_existing(
                appointment_id=str(original.id), new_appointment_datetime=slot_b
            )

    rolled_back = await Appointment.get(original.id)
    assert rolled_back.status == AppointmentStatus.rescheduled
    # Rolled back to "synced" (its real pre-attempt value) — NOT left at "pending", which would
    # incorrectly queue a Google Calendar event deletion for an appointment that's actually
    # still active.
    assert rolled_back.google_calendar_sync_status == "synced"


@pytest.mark.asyncio
async def test_cancel_queues_google_calendar_deletion(business_config: BusinessConfig):
    slot = _next_monday_9am()
    person_id = await _make_person("+15550000009")
    appointment = await appointment_service.book_first_time(
        person_id=person_id, appointment_datetime=slot, booking_source=BookingSource.inbound_call
    )

    cancelled = await appointment_service.cancel(str(appointment.id), reason="Patient request")
    assert cancelled.status == AppointmentStatus.cancelled
    assert cancelled.google_calendar_sync_status == "pending"
