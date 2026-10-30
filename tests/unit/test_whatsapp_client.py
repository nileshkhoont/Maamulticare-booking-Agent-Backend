"""Covers WhatsAppClient's retry/backoff behavior and config-error paths. Network calls are
mocked at httpx.AsyncClient.request (not via a real HTTP transport).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.config import settings
from app.core.exceptions import WhatsAppIntegrationError
from app.integrations.whatsapp.client import WhatsAppClient


def _response(status_code: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=body if body is not None else {},
        request=httpx.Request("POST", "https://graph.facebook.com/v25.0/test"),
    )


@pytest.fixture
def client() -> WhatsAppClient:
    return WhatsAppClient()


@pytest.mark.asyncio
async def test_send_template_message_success(client: WhatsAppClient):
    mock_request = AsyncMock(
        return_value=_response(200, {"messages": [{"id": "wamid.ABC123"}]})
    )
    original_phone_number_id = settings.meta_whatsapp_phone_number_id
    settings.meta_whatsapp_phone_number_id = "test-phone-number-id"
    try:
        with patch("httpx.AsyncClient.request", mock_request):
            result = await client.send_template_message(
                to="919876543210",
                template_name="appointment_booked_v1",
                language_code="en_US",
                body_parameters=["Priya Shah", "14 Oct 2026, 03:00 PM"],
            )
    finally:
        settings.meta_whatsapp_phone_number_id = original_phone_number_id

    assert result.message_id == "wamid.ABC123"
    call_kwargs = mock_request.call_args.kwargs
    body = call_kwargs["json"]
    assert body["messaging_product"] == "whatsapp"
    assert body["to"] == "919876543210"
    assert body["type"] == "template"
    assert body["template"]["name"] == "appointment_booked_v1"
    assert body["template"]["language"] == {"code": "en_US"}
    assert body["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Priya Shah"},
        {"type": "text", "text": "14 Oct 2026, 03:00 PM"},
    ]
    assert "test-phone-number-id" in str(mock_request.call_args.args[1])


@pytest.mark.asyncio
async def test_send_template_message_raises_if_phone_number_id_unset(client: WhatsAppClient):
    original = settings.meta_whatsapp_phone_number_id
    settings.meta_whatsapp_phone_number_id = None
    try:
        with pytest.raises(WhatsAppIntegrationError, match="META_WHATSAPP_PHONE_NUMBER_ID"):
            await client.send_template_message(
                to="919876543210", template_name="x", language_code="en_US", body_parameters=[]
            )
    finally:
        settings.meta_whatsapp_phone_number_id = original


@pytest.mark.asyncio
async def test_send_template_message_raises_if_access_token_unset(client: WhatsAppClient):
    original_token = settings.meta_whatsapp_access_token
    original_phone_number_id = settings.meta_whatsapp_phone_number_id
    settings.meta_whatsapp_access_token = None
    settings.meta_whatsapp_phone_number_id = "test-phone-number-id"
    try:
        with pytest.raises(WhatsAppIntegrationError, match="META_WHATSAPP_ACCESS_TOKEN"):
            await client.send_template_message(
                to="919876543210", template_name="x", language_code="en_US", body_parameters=[]
            )
    finally:
        settings.meta_whatsapp_access_token = original_token
        settings.meta_whatsapp_phone_number_id = original_phone_number_id


@pytest.mark.asyncio
async def test_send_template_message_non_retryable_4xx_raises_immediately(client: WhatsAppClient):
    mock_request = AsyncMock(return_value=_response(400, {"error": "bad request"}))
    original = settings.meta_whatsapp_phone_number_id
    settings.meta_whatsapp_phone_number_id = "test-phone-number-id"
    try:
        with patch("httpx.AsyncClient.request", mock_request):
            with pytest.raises(WhatsAppIntegrationError):
                await client.send_template_message(
                    to="919876543210", template_name="x", language_code="en_US", body_parameters=[]
                )
    finally:
        settings.meta_whatsapp_phone_number_id = original
    assert mock_request.call_count == 1  # retryable=False — never retried


@pytest.mark.asyncio
async def test_send_template_message_503_is_not_retried(client: WhatsAppClient):
    """retryable=False really means zero retries for this call — a blind retry after an
    ambiguous failure risks genuinely double-messaging the patient (see client.py's comment).
    """
    mock_request = AsyncMock(return_value=_response(503))
    original = settings.meta_whatsapp_phone_number_id
    settings.meta_whatsapp_phone_number_id = "test-phone-number-id"
    try:
        with patch("httpx.AsyncClient.request", mock_request), patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(WhatsAppIntegrationError):
                await client.send_template_message(
                    to="919876543210", template_name="x", language_code="en_US", body_parameters=[]
                )
    finally:
        settings.meta_whatsapp_phone_number_id = original
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_send_template_message_missing_id_in_response_raises(client: WhatsAppClient):
    mock_request = AsyncMock(return_value=_response(200, {"messages": [{}]}))
    original = settings.meta_whatsapp_phone_number_id
    settings.meta_whatsapp_phone_number_id = "test-phone-number-id"
    try:
        with patch("httpx.AsyncClient.request", mock_request):
            with pytest.raises(WhatsAppIntegrationError, match="messages\\[0\\].id"):
                await client.send_template_message(
                    to="919876543210", template_name="x", language_code="en_US", body_parameters=[]
                )
    finally:
        settings.meta_whatsapp_phone_number_id = original
