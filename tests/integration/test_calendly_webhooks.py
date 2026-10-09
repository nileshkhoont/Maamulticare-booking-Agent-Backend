"""Signature verification + event-handling tests for the Calendly webhook receiver. Mirrors
tests/integration/test_edesy_webhooks.py's conventions exactly — see that file's own docstring.

calendly_client's network-hitting methods (get_scheduled_event) are mocked via unittest.mock at
the import location services/calendly_sync_service.py actually uses, since a real call would hit
the live Calendly API (no real account reachable in tests, and would be a real side effect even
if one were).
"""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.integrations.calendly.schemas import CalendlyScheduledEventResource, CalendlyScheduledEventResponse
from app.models.appointment import Appointment
from app.models.person import Person


def _sign(body: bytes, timestamp: int | None = None) -> str:
    timestamp = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{timestamp}.{body.decode()}".encode()
    signature = hmac.new(
        settings.calendly_webhook_signing_key.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={signature}"


async def _post_webhook(client: AsyncClient, payload: dict, sign: bool = True, stale: bool = False):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        timestamp = int(time.time()) - 3600 if stale else None
        headers["Calendly-Webhook-Signature"] = _sign(body, timestamp)
    return await client.post("/api/v1/webhooks/calendly", content=body, headers=headers)


def _invitee_payload(
    event_name: str,
    uri: str,
    event_uri: str,
    email: str = "patient@example.com",
    name: str = "Jordan Lee",
    phone: str = "+919876500001",
    rescheduled: bool = False,
    old_invitee: str | None = None,
    new_invitee: str | None = None,
    cancellation: dict | None = None,
) -> dict:
    return {
        "event": event_name,
        "created_at": "2026-10-08T10:00:00.000000Z",
        "payload": {
            "uri": uri,
            "event": event_uri,
            "email": email,
            "name": name,
            "status": "canceled" if event_name == "invitee.canceled" else "active",
            "timezone": "Asia/Kolkata",
            "text_reminder_number": phone,
            "rescheduled": rescheduled,
            "old_invitee": old_invitee,
            "new_invitee": new_invitee,
            "cancellation": cancellation,
        },
    }


def _mock_scheduled_event(start_time: str, end_time: str):
    return AsyncMock(
        return_value=CalendlyScheduledEventResponse(
            resource=CalendlyScheduledEventResource(
                uri="https://api.calendly.com/scheduled_events/evt-1", start_time=start_time, end_time=end_time
            )
        )
    )


@pytest.mark.asyncio
async def test_webhook_rejects_missing_signature(client: AsyncClient):
    response = await _post_webhook(client, {"event": "invitee.created"}, sign=False)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature(client: AsyncClient):
    body = json.dumps({"event": "invitee.created"}).encode()
    response = await client.post(
        "/api/v1/webhooks/calendly",
        content=body,
        headers={"Calendly-Webhook-Signature": "t=123,v1=not-the-real-signature", "Content-Type": "application/json"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_rejects_stale_timestamp(client: AsyncClient):
    payload = _invitee_payload("invitee.created", "uri-1", "event-1")
    response = await _post_webhook(client, payload, stale=True)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_webhook_echo_ignored_for_own_api_booking(client: AsyncClient):
    """A webhook for an invitee URI we've already recorded (our own push's confirmation) must
    never create a second local Appointment.
    """
    person = Person(full_name="Harsh", phone_number="+917600181441")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-10-15T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_invitee_uri="https://api.calendly.com/scheduled_events/evt-1/invitees/inv-1",
        calendly_event_uri="https://api.calendly.com/scheduled_events/evt-1",
        calendly_sync_status="synced",
    )
    await appointment.insert()

    payload = _invitee_payload(
        "invitee.created",
        uri="https://api.calendly.com/scheduled_events/evt-1/invitees/inv-1",
        event_uri="https://api.calendly.com/scheduled_events/evt-1",
    )
    with patch("app.services.calendly_sync_service.calendly_client.get_scheduled_event") as mock_get:
        response = await _post_webhook(client, payload)
    assert response.status_code == 200
    mock_get.assert_not_called()  # echo check short-circuits before any API call is needed

    count = await Appointment.find(Appointment.person_id == str(person.id)).count()
    assert count == 1


@pytest.mark.asyncio
async def test_webhook_race_guard_self_heals_instead_of_duplicating(client: AsyncClient):
    """A webhook can arrive before our own push's response has been written back onto the local
    row (calendly_invitee_uri still None) — must self-heal that row, not create a duplicate.
    """
    person = Person(full_name="Rakesh", phone_number="+919876500002")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-10-20T06:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_sync_status="pending",
    )
    await appointment.insert()

    payload = _invitee_payload(
        "invitee.created",
        uri="https://api.calendly.com/scheduled_events/evt-2/invitees/inv-2",
        event_uri="https://api.calendly.com/scheduled_events/evt-2",
        phone="+919876500002",
    )
    mock_event = _mock_scheduled_event("2026-10-20T06:00:00.000000Z", "2026-10-20T06:30:00.000000Z")
    with patch("app.services.calendly_sync_service.calendly_client.get_scheduled_event", mock_event):
        response = await _post_webhook(client, payload)
    assert response.status_code == 200

    count = await Appointment.find(Appointment.person_id == str(person.id)).count()
    assert count == 1
    healed = await Appointment.get(appointment.id)
    assert healed.calendly_invitee_uri == "https://api.calendly.com/scheduled_events/evt-2/invitees/inv-2"
    assert healed.calendly_sync_status == "synced"


@pytest.mark.asyncio
async def test_webhook_creates_local_appointment_for_direct_calendly_booking(client: AsyncClient):
    """A genuinely new booking made directly in Calendly (no old_invitee, no matching local row)
    must create a local Appointment, tagged calendly_direct on both fields.
    """
    payload = _invitee_payload(
        "invitee.created",
        uri="https://api.calendly.com/scheduled_events/evt-3/invitees/inv-3",
        event_uri="https://api.calendly.com/scheduled_events/evt-3",
        phone="+919876500003",
        email="newpatient@example.com",
        name="New Patient",
    )
    mock_event = _mock_scheduled_event("2026-10-22T07:00:00.000000Z", "2026-10-22T07:30:00.000000Z")
    with patch("app.services.calendly_sync_service.calendly_client.get_scheduled_event", mock_event):
        response = await _post_webhook(client, payload)
    assert response.status_code == 200

    appointment = await Appointment.find_one(
        Appointment.calendly_invitee_uri == "https://api.calendly.com/scheduled_events/evt-3/invitees/inv-3"
    )
    assert appointment is not None
    assert appointment.status == "booked"
    assert appointment.booking_source == "calendly_direct"
    assert appointment.scheduling_method == "calendly_direct"
    assert appointment.duration_minutes == 30

    person = await Person.find_one(Person.phone_number == "+919876500003")
    assert person is not None


@pytest.mark.asyncio
async def test_webhook_cancellation_is_idempotent(client: AsyncClient):
    person = Person(full_name="Meet", phone_number="+919876500004")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-10-18T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_invitee_uri="https://api.calendly.com/scheduled_events/evt-4/invitees/inv-4",
        calendly_event_uri="https://api.calendly.com/scheduled_events/evt-4",
        calendly_sync_status="synced",
    )
    await appointment.insert()

    payload = _invitee_payload(
        "invitee.canceled",
        uri="https://api.calendly.com/scheduled_events/evt-4/invitees/inv-4",
        event_uri="https://api.calendly.com/scheduled_events/evt-4",
        cancellation={"reason": "Can't make it", "canceled_by": "invitee"},
    )

    first = await _post_webhook(client, payload)
    assert first.status_code == 200
    second = await _post_webhook(client, payload)  # duplicate delivery
    assert second.status_code == 200

    cancelled = await Appointment.get(appointment.id)
    assert cancelled.status == "cancelled"
    # Note appended exactly once, not twice, despite the duplicate delivery.
    assert cancelled.notes.count("Cancelled via Calendly") == 1


@pytest.mark.asyncio
async def test_webhook_reschedule_pair_new_arrives_first(client: AsyncClient):
    """invitee.created (the new half, with old_invitee set) arriving BEFORE the matching
    invitee.canceled must still correctly cancel the old row proactively.
    """
    person = Person(full_name="Dhrumil", phone_number="+919876500005")
    await person.insert()
    old_appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-10-25T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_invitee_uri="https://api.calendly.com/scheduled_events/evt-5/invitees/inv-5-old",
        calendly_event_uri="https://api.calendly.com/scheduled_events/evt-5",
        calendly_sync_status="synced",
    )
    await old_appointment.insert()

    created_payload = _invitee_payload(
        "invitee.created",
        uri="https://api.calendly.com/scheduled_events/evt-5-new/invitees/inv-5-new",
        event_uri="https://api.calendly.com/scheduled_events/evt-5-new",
        phone="+919876500005",
        old_invitee="https://api.calendly.com/scheduled_events/evt-5/invitees/inv-5-old",
    )
    mock_event = _mock_scheduled_event("2026-10-26T06:00:00.000000Z", "2026-10-26T06:30:00.000000Z")
    with patch("app.services.calendly_sync_service.calendly_client.get_scheduled_event", mock_event):
        response = await _post_webhook(client, created_payload)
    assert response.status_code == 200

    old_refreshed = await Appointment.get(old_appointment.id)
    assert old_refreshed.status == "cancelled"

    new_appointment = await Appointment.find_one(
        Appointment.calendly_invitee_uri == "https://api.calendly.com/scheduled_events/evt-5-new/invitees/inv-5-new"
    )
    assert new_appointment is not None
    assert new_appointment.status == "rescheduled"
    assert new_appointment.original_appointment_id == str(old_appointment.id)

    # Now the paired invitee.canceled for the OLD invitee arrives — must no-op (already cancelled).
    canceled_payload = _invitee_payload(
        "invitee.canceled",
        uri="https://api.calendly.com/scheduled_events/evt-5/invitees/inv-5-old",
        event_uri="https://api.calendly.com/scheduled_events/evt-5",
        rescheduled=True,
        new_invitee="https://api.calendly.com/scheduled_events/evt-5-new/invitees/inv-5-new",
    )
    second_response = await _post_webhook(client, canceled_payload)
    assert second_response.status_code == 200
    still_cancelled = await Appointment.get(old_appointment.id)
    assert still_cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_webhook_reschedule_pair_old_arrives_first(client: AsyncClient):
    """invitee.canceled (the old half) arriving BEFORE the matching invitee.created must simply
    cancel the old row and wait — the new row is created once its own invitee.created lands.
    """
    person = Person(full_name="Subhashbhai", phone_number="+919876500006")
    await person.insert()
    old_appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime="2026-10-27T05:00:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        scheduling_method="api",
        calendly_invitee_uri="https://api.calendly.com/scheduled_events/evt-6/invitees/inv-6-old",
        calendly_event_uri="https://api.calendly.com/scheduled_events/evt-6",
        calendly_sync_status="synced",
    )
    await old_appointment.insert()

    canceled_payload = _invitee_payload(
        "invitee.canceled",
        uri="https://api.calendly.com/scheduled_events/evt-6/invitees/inv-6-old",
        event_uri="https://api.calendly.com/scheduled_events/evt-6",
        rescheduled=True,
        new_invitee="https://api.calendly.com/scheduled_events/evt-6-new/invitees/inv-6-new",
    )
    first = await _post_webhook(client, canceled_payload)
    assert first.status_code == 200
    cancelled_now = await Appointment.get(old_appointment.id)
    assert cancelled_now.status == "cancelled"

    created_payload = _invitee_payload(
        "invitee.created",
        uri="https://api.calendly.com/scheduled_events/evt-6-new/invitees/inv-6-new",
        event_uri="https://api.calendly.com/scheduled_events/evt-6-new",
        phone="+919876500006",
        old_invitee="https://api.calendly.com/scheduled_events/evt-6/invitees/inv-6-old",
    )
    mock_event = _mock_scheduled_event("2026-10-28T06:00:00.000000Z", "2026-10-28T06:30:00.000000Z")
    with patch("app.services.calendly_sync_service.calendly_client.get_scheduled_event", mock_event):
        second = await _post_webhook(client, created_payload)
    assert second.status_code == 200

    new_appointment = await Appointment.find_one(
        Appointment.calendly_invitee_uri == "https://api.calendly.com/scheduled_events/evt-6-new/invitees/inv-6-new"
    )
    assert new_appointment is not None
    assert new_appointment.status == "rescheduled"
    assert new_appointment.original_appointment_id == str(old_appointment.id)
