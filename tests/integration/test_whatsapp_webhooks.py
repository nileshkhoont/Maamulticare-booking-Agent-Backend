"""Signature verification + status-event handling tests for the Meta WhatsApp webhook receiver."""

import hashlib
import hmac
import json

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.person import Person


def _sign(body: bytes) -> str:
    digest = hmac.new(settings.meta_whatsapp_app_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _post_webhook(client: AsyncClient, payload: dict, sign: bool = True):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(body)
    return await client.post("/api/v1/webhooks/whatsapp", content=body, headers=headers)


def _status_payload(message_id: str, status: str, errors: list[dict] | None = None) -> dict:
    value: dict = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001111", "phone_number_id": "test-phone-number-id"},
        "statuses": [
            {
                "id": message_id,
                "status": status,
                "timestamp": "1700000000",
                "recipient_id": "919876543210",
            }
        ],
    }
    if errors:
        value["statuses"][0]["errors"] = errors
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba-1", "changes": [{"field": "messages", "value": value}]}]}


def _inbound_message_payload(from_number: str) -> dict:
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001111", "phone_number_id": "test-phone-number-id"},
        "messages": [{"from": from_number, "id": "wamid.INBOUND", "timestamp": "1700000000", "type": "text", "text": {"body": "hi"}}],
    }
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba-1", "changes": [{"field": "messages", "value": value}]}]}


@pytest.mark.asyncio
async def test_get_verification_challenge_echoed_on_matching_token(client: AsyncClient):
    response = await client.get(
        "/api/v1/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.meta_whatsapp_webhook_verify_token,
            "hub.challenge": "challenge-123",
        },
    )
    assert response.status_code == 200
    assert response.text == "challenge-123"


@pytest.mark.asyncio
async def test_get_verification_rejected_on_mismatched_token(client: AsyncClient):
    response = await client.get(
        "/api/v1/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong-token", "hub.challenge": "challenge-123"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_post_rejects_missing_signature(client: AsyncClient):
    response = await _post_webhook(client, _status_payload("wamid.X", "sent"), sign=False)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_post_rejects_invalid_signature(client: AsyncClient):
    body = json.dumps(_status_payload("wamid.X", "sent")).encode()
    response = await client.post(
        "/api/v1/webhooks/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=not-the-right-signature", "Content-Type": "application/json"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_post_valid_status_event_updates_matching_appointment(client: AsyncClient):
    person = Person(phone_number="+919876543210")
    await person.insert()
    from datetime import UTC, datetime, timedelta

    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime=datetime.now(UTC) + timedelta(days=1),
        status="booked",
        booking_source="inbound_call",
        whatsapp_notification_type="booked",
        whatsapp_notification_status="sent",
        whatsapp_message_id="wamid.REAL",
    )
    await appointment.insert()

    response = await _post_webhook(client, _status_payload("wamid.REAL", "delivered"))
    assert response.status_code == 200

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "delivered"


@pytest.mark.asyncio
async def test_post_failed_status_captures_error_detail(client: AsyncClient):
    person = Person(phone_number="+919876543211")
    await person.insert()
    from datetime import UTC, datetime, timedelta

    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime=datetime.now(UTC) + timedelta(days=1),
        status="booked",
        booking_source="inbound_call",
        whatsapp_notification_type="booked",
        whatsapp_notification_status="sent",
        whatsapp_message_id="wamid.FAILME",
    )
    await appointment.insert()

    payload = _status_payload(
        "wamid.FAILME", "failed", errors=[{"code": 131026, "title": "Message undeliverable"}]
    )
    response = await _post_webhook(client, payload)
    assert response.status_code == 200

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "failed"
    assert refreshed.whatsapp_notification_error == "Message undeliverable"


@pytest.mark.asyncio
async def test_post_inbound_message_ignored_not_acted_on(client: AsyncClient):
    """Inbound patient replies are logged, never processed — this is not a chatbot."""
    response = await _post_webhook(client, _inbound_message_payload("+919876543212"))
    assert response.status_code == 200
    # No appointment exists to have been touched — this just proves the endpoint doesn't error
    # or attempt to look anything up for a messages[] entry.


@pytest.mark.asyncio
async def test_post_malformed_json_returns_200(client: AsyncClient):
    body = b"{not valid json"
    response = await client.post(
        "/api/v1/webhooks/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_post_unmatched_message_id_does_not_error(client: AsyncClient):
    response = await _post_webhook(client, _status_payload("wamid.NOBODY", "delivered"))
    assert response.status_code == 200
