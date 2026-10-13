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

    # The original slot should be free again — someone else can now book it.
    reclaimed = await appointment_service.book_first_time(
        person_id=person2_id, appointment_datetime=original_slot, booking_source=BookingSource.inbound_call
    )
    assert reclaimed.status == AppointmentStatus.booked
