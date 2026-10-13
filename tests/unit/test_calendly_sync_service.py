"""Direct-service-call tests for calendly_sync_service's outbound push logic, following
tests/unit/test_appointment_service.py's style (no HTTP layer). calendly_client's network calls
are mocked throughout.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.calendly.schemas import CalendlyCreateInviteeResponse, CalendlyInviteeResource
from app.models.appointment import Appointment
from app.models.person import Person
from app.services.calendly_sync_service import calendly_sync_service


@pytest.mark.asyncio
async def test_push_booking_uses_real_email_when_present(business_config):
    person = Person(full_name="Harsh", phone_number="+917600181441", email="harsh@example.com")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-01T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock(
        return_value=CalendlyCreateInviteeResponse(
            resource=CalendlyInviteeResource(
                uri="https://api.calendly.com/scheduled_events/evt-u1/invitees/inv-u1",
                event="https://api.calendly.com/scheduled_events/evt-u1",
            )
        )
    )
    with patch("app.services.calendly_sync_service.calendly_client.create_invitee", mock_create):
        await calendly_sync_service.process_pending_appointment(appointment)

    sent_request = mock_create.call_args.args[0]
    assert sent_request.invitee.email == "harsh@example.com"
    assert sent_request.invitee.text_reminder_number == "+917600181441"

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.is_placeholder_email is False
    assert refreshed.calendly_sync_status == "synced"
    assert refreshed.calendly_invitee_uri == "https://api.calendly.com/scheduled_events/evt-u1/invitees/inv-u1"


@pytest.mark.asyncio
async def test_push_booking_uses_placeholder_email_when_person_has_none(business_config):
    person = Person(full_name="Nisarg", phone_number="+916354145435")  # no email
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-02T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock(
        return_value=CalendlyCreateInviteeResponse(
            resource=CalendlyInviteeResource(
                uri="https://api.calendly.com/scheduled_events/evt-u2/invitees/inv-u2",
                event="https://api.calendly.com/scheduled_events/evt-u2",
            )
        )
    )
    with patch("app.services.calendly_sync_service.calendly_client.create_invitee", mock_create):
        await calendly_sync_service.process_pending_appointment(appointment)

    sent_request = mock_create.call_args.args[0]
    assert sent_request.invitee.email == "harshjagani@movya.com"
    assert sent_request.invitee.text_reminder_number == "+916354145435"

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.is_placeholder_email is True
    assert refreshed.calendly_sync_status == "synced"


@pytest.mark.asyncio
async def test_push_booking_includes_location_when_configured(business_config):
    """The Event Type (Consultation - Dr. Shyani) is configured for an in-person location in the
    real Calendly dashboard — POST /invitees must carry a matching `location` object or Calendly
    rejects the request. Confirmed via calendly_event_location_kind/_text settings.
    """
    from app.core.config import settings

    person = Person(full_name="Priya", phone_number="+919825000011", email="priya@example.com")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-06T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock(
        return_value=CalendlyCreateInviteeResponse(
            resource=CalendlyInviteeResource(
                uri="https://api.calendly.com/scheduled_events/evt-loc/invitees/inv-loc",
                event="https://api.calendly.com/scheduled_events/evt-loc",
            )
        )
    )
    original_kind, original_text = settings.calendly_event_location_kind, settings.calendly_event_location_text
    settings.calendly_event_location_kind = "physical"
    settings.calendly_event_location_text = "Maa MultiCare Hospital, Nikol, Ahmedabad"
    try:
        with patch("app.services.calendly_sync_service.calendly_client.create_invitee", mock_create):
            await calendly_sync_service.process_pending_appointment(appointment)
    finally:
        settings.calendly_event_location_kind = original_kind
        settings.calendly_event_location_text = original_text

    sent_request = mock_create.call_args.args[0]
    assert sent_request.location is not None
    assert sent_request.location.kind == "physical"
    assert sent_request.location.location == "Maa MultiCare Hospital, Nikol, Ahmedabad"


@pytest.mark.asyncio
async def test_push_booking_omits_location_when_not_configured(business_config):
    """When the Event Type has no location configured (calendly_event_location_kind unset, the
    test-env default), POST /invitees must omit the field entirely — Calendly rejects a location
    object sent for an Event Type that doesn't specify one.
    """
    person = Person(full_name="Raj", phone_number="+919825000012", email="raj@example.com")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-07T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock(
        return_value=CalendlyCreateInviteeResponse(
            resource=CalendlyInviteeResource(
                uri="https://api.calendly.com/scheduled_events/evt-noloc/invitees/inv-noloc",
                event="https://api.calendly.com/scheduled_events/evt-noloc",
            )
        )
    )
    with patch("app.services.calendly_sync_service.calendly_client.create_invitee", mock_create):
        await calendly_sync_service.process_pending_appointment(appointment)

    sent_request = mock_create.call_args.args[0]
    assert sent_request.location is None


@pytest.mark.asyncio
async def test_push_booking_skips_if_already_has_invitee_uri(business_config):
    """A row re-queued (e.g. by the reconciliation sweep) that already has a calendly_invitee_uri
    must not be pushed a second time.
    """
    person = Person(full_name="Meet", phone_number="+919825690120")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-03T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_invitee_uri="https://api.calendly.com/scheduled_events/evt-u3/invitees/inv-u3",
        calendly_event_uri="https://api.calendly.com/scheduled_events/evt-u3",
        calendly_sync_status="pending",
    )
    await appointment.insert()

    mock_create = AsyncMock()
    with patch("app.services.calendly_sync_service.calendly_client.create_invitee", mock_create):
        await calendly_sync_service.process_pending_appointment(appointment)

    mock_create.assert_not_called()
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.calendly_sync_status == "synced"


@pytest.mark.asyncio
async def test_push_cancellation_calls_cancel_endpoint_with_event_uuid(business_config):
    person = Person(full_name="Harsh", phone_number="+917600181441")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-04T05:00:00+00:00",
        status="cancelled",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_invitee_uri="https://api.calendly.com/scheduled_events/evt-u4/invitees/inv-u4",
        calendly_event_uri="https://api.calendly.com/scheduled_events/evt-u4",
        calendly_sync_status="pending",
    )
    await appointment.insert()

    mock_cancel = AsyncMock()
    with patch("app.services.calendly_sync_service.calendly_client.cancel_scheduled_event", mock_cancel):
        await calendly_sync_service.process_pending_appointment(appointment)

    mock_cancel.assert_called_once()
    assert mock_cancel.call_args.args[0] == "evt-u4"  # uuid extracted from the event URI

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.calendly_sync_status == "synced"


@pytest.mark.asyncio
async def test_push_cancellation_is_not_applicable_if_never_reached_calendly(business_config):
    """A cancelled appointment that never had a calendly_event_uri (push never completed, or a
    pre-integration row) has nothing to cancel on Calendly's side.
    """
    person = Person(full_name="Harsh", phone_number="+917600181441")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-11-05T05:00:00+00:00",
        status="cancelled",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_sync_status="pending",
    )
    await appointment.insert()

    mock_cancel = AsyncMock()
    with patch("app.services.calendly_sync_service.calendly_client.cancel_scheduled_event", mock_cancel):
        await calendly_sync_service.process_pending_appointment(appointment)

    mock_cancel.assert_not_called()
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.calendly_sync_status == "not_applicable"


@pytest.mark.asyncio
async def test_resolve_person_prefers_phone_over_email():
    payload_person = await calendly_sync_service._resolve_person(
        _FakePayload(text_reminder_number="+919876500099", email="someone@example.com", name="Someone")
    )
    assert payload_person is not None
    assert payload_person.phone_number == "+919876500099"


@pytest.mark.asyncio
async def test_resolve_person_falls_back_to_email_when_no_phone():
    existing = Person(full_name="Email Only", phone_number="+919999999999", email="emailonly@example.com")
    await existing.insert()
    payload_person = await calendly_sync_service._resolve_person(
        _FakePayload(text_reminder_number=None, email="emailonly@example.com", name="Email Only")
    )
    assert payload_person is not None
    assert str(payload_person.id) == str(existing.id)


@pytest.mark.asyncio
async def test_resolve_person_returns_none_when_unresolvable():
    payload_person = await calendly_sync_service._resolve_person(
        _FakePayload(text_reminder_number=None, email=None, name="Nobody")
    )
    assert payload_person is None


class _FakePayload:
    """Minimal stand-in for CalendlyWebhookInviteePayload's fields _resolve_person reads."""

    def __init__(self, text_reminder_number, email, name):
        self.text_reminder_number = text_reminder_number
        self.email = email
        self.name = name
        self.uri = "https://api.calendly.com/scheduled_events/evt-x/invitees/inv-x"
