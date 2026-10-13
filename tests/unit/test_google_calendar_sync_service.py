"""Direct-service-call tests for google_calendar_sync_service's outbound push logic, following
tests/unit/test_appointment_service.py's style (no HTTP layer). google_calendar_client's network
calls are mocked throughout.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.integrations.google_calendar.schemas import GoogleCalendarEvent
from app.models.appointment import Appointment
from app.models.business_config import BusinessConfig
from app.models.person import Person
from app.services.google_calendar_sync_service import google_calendar_sync_service


@pytest.mark.asyncio
async def test_push_booking_success_sends_expected_fields(business_config):
    person = Person(full_name="Harsh", phone_number="+917600181441")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-01T05:00:00+00:00",
        duration_minutes=20,
        status="booked",
        booking_source="admin_scheduled_call",
        notes="Back pain, 2 weeks",
        google_calendar_sync_status="pending",
    )
    await appointment.insert()

    original_location = settings.google_calendar_event_location
    settings.google_calendar_event_location = "Maa MultiCare Hospital, Nikol, Ahmedabad"
    mock_create = AsyncMock(
        return_value=GoogleCalendarEvent(id=str(appointment.id), summary="Appointment: Harsh")
    )
    try:
        with patch("app.services.google_calendar_sync_service.google_calendar_client.create_event", mock_create):
            await google_calendar_sync_service.process_pending_appointment(appointment)
    finally:
        settings.google_calendar_event_location = original_location

    sent_event = mock_create.call_args.args[0]
    assert sent_event.id == str(appointment.id)
    assert "Harsh" in sent_event.summary
    assert "Harsh" in sent_event.description
    assert "+917600181441" in sent_event.description
    assert "Back pain, 2 weeks" in sent_event.description
    assert sent_event.location == "Maa MultiCare Hospital, Nikol, Ahmedabad"
    assert sent_event.start.timeZone == "UTC"  # business_config fixture's own timezone
    assert sent_event.end is not None

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.google_calendar_event_id == str(appointment.id)
    assert refreshed.google_calendar_sync_status == "synced"
    assert refreshed.google_calendar_last_synced_at is not None


@pytest.mark.asyncio
async def test_push_booking_falls_back_to_default_timezone_when_business_config_missing():
    """No BusinessConfig document at all (not even one with a blank timezone) — must fall back
    to Asia/Kolkata rather than crashing.
    """
    person = Person(full_name="NoConfig", phone_number="+917600181442")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-01T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        google_calendar_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock(return_value=GoogleCalendarEvent(id=str(appointment.id)))
    with patch("app.services.google_calendar_sync_service.google_calendar_client.create_event", mock_create):
        await google_calendar_sync_service.process_pending_appointment(appointment)

    sent_event = mock_create.call_args.args[0]
    assert sent_event.start.timeZone == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_push_booking_falls_back_on_invalid_business_config_timezone():
    config = BusinessConfig(
        working_days=["monday"],
        working_hours=[],
        slot_duration_minutes=30,
        buffer_minutes=0,
        holidays=[],
        max_advance_booking_days=30,
        timezone="Not/A_Real_Timezone",
    )
    await config.insert()

    person = Person(full_name="BadTz", phone_number="+917600181443")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-01T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        google_calendar_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock(return_value=GoogleCalendarEvent(id=str(appointment.id)))
    with patch("app.services.google_calendar_sync_service.google_calendar_client.create_event", mock_create):
        await google_calendar_sync_service.process_pending_appointment(appointment)  # must not crash

    sent_event = mock_create.call_args.args[0]
    assert sent_event.start.timeZone == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_push_booking_uses_default_duration_when_unset(business_config):
    person = Person(full_name="NoDuration", phone_number="+917600181444")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-01T05:00:00+00:00",
        duration_minutes=None,
        status="booked",
        booking_source="admin_scheduled_call",
        google_calendar_sync_status="pending",
    )
    await appointment.insert()

    original_default = settings.google_calendar_default_event_duration_minutes
    settings.google_calendar_default_event_duration_minutes = 45
    mock_create = AsyncMock(return_value=GoogleCalendarEvent(id=str(appointment.id)))
    try:
        with patch("app.services.google_calendar_sync_service.google_calendar_client.create_event", mock_create):
            await google_calendar_sync_service.process_pending_appointment(appointment)
    finally:
        settings.google_calendar_default_event_duration_minutes = original_default

    sent_event = mock_create.call_args.args[0]
    start_dt = sent_event.start.dateTime
    end_dt = sent_event.end.dateTime
    # "2026-11-01T05:00:00+00:00" + 45min, compared as strings is fragile — parse instead.
    from datetime import datetime

    start = datetime.fromisoformat(start_dt)
    end = datetime.fromisoformat(end_dt)
    assert (end - start).total_seconds() == 45 * 60


@pytest.mark.asyncio
async def test_push_booking_skips_if_already_has_event_id(business_config):
    person = Person(full_name="AlreadySynced", phone_number="+917600181445")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-02T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        google_calendar_event_id="existing-event-id",
        google_calendar_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock()
    with patch("app.services.google_calendar_sync_service.google_calendar_client.create_event", mock_create):
        await google_calendar_sync_service.process_pending_appointment(appointment)

    mock_create.assert_not_called()
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.google_calendar_sync_status == "synced"


@pytest.mark.asyncio
async def test_push_booking_missing_person_marks_failed(business_config):
    appointment = Appointment(
        person_id="000000000000000000000000",  # well-formed but nonexistent ObjectId
        appointment_datetime="2026-11-02T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        google_calendar_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock()
    with patch("app.services.google_calendar_sync_service.google_calendar_client.create_event", mock_create):
        await google_calendar_sync_service.process_pending_appointment(appointment)

    mock_create.assert_not_called()
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.google_calendar_sync_status == "failed"
    assert refreshed.google_calendar_sync_error is not None


@pytest.mark.asyncio
async def test_push_cancellation_success(business_config):
    person = Person(full_name="ToCancel", phone_number="+917600181446")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-03T05:00:00+00:00",
        status="cancelled",
        booking_source="admin_scheduled_call",
        google_calendar_event_id="evt-to-delete",
        google_calendar_sync_status="pending",
    )
    await appointment.insert()

    mock_delete = AsyncMock()
    with patch("app.services.google_calendar_sync_service.google_calendar_client.delete_event", mock_delete):
        await google_calendar_sync_service.process_pending_appointment(appointment)

    mock_delete.assert_called_once_with("evt-to-delete")
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.google_calendar_sync_status == "synced"


@pytest.mark.asyncio
async def test_push_cancellation_not_applicable_if_never_synced(business_config):
    person = Person(full_name="NeverSynced", phone_number="+917600181447")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-04T05:00:00+00:00",
        status="cancelled",
        booking_source="admin_scheduled_call",
        google_calendar_sync_status="pending",
    )
    await appointment.insert()

    mock_delete = AsyncMock()
    with patch("app.services.google_calendar_sync_service.google_calendar_client.delete_event", mock_delete):
        await google_calendar_sync_service.process_pending_appointment(appointment)

    mock_delete.assert_not_called()
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.google_calendar_sync_status == "not_applicable"


@pytest.mark.asyncio
async def test_process_pending_appointment_never_raises_past_integration_error(business_config):
    from app.core.exceptions import GoogleCalendarIntegrationError

    person = Person(full_name="WillFail", phone_number="+917600181448")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-05T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        google_calendar_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock(side_effect=GoogleCalendarIntegrationError("simulated failure"))
    with patch("app.services.google_calendar_sync_service.google_calendar_client.create_event", mock_create):
        await google_calendar_sync_service.process_pending_appointment(appointment)  # must not raise

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.google_calendar_sync_status == "failed"
    assert "simulated failure" in refreshed.google_calendar_sync_error
