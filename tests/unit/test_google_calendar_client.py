"""Covers GoogleCalendarClient's retry/backoff behavior and credential-loading error paths.
Network calls are mocked at httpx.AsyncClient.request (not via a real HTTP transport), and
_headers() is patched to a fixed test token so these tests never touch real Google auth.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.config import settings
from app.core.exceptions import GoogleCalendarIntegrationError
from app.integrations.google_calendar.client import GoogleCalendarClient
from app.integrations.google_calendar.schemas import GoogleCalendarEvent


def _response(status_code: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=body if body is not None else {},
        request=httpx.Request("POST", "https://www.googleapis.com/calendar/v3/test"),
    )


@pytest.fixture
def client_with_fake_auth():
    client = GoogleCalendarClient()
    with patch.object(client, "_headers", AsyncMock(return_value={"Authorization": "Bearer test-token"})):
        yield client


@pytest.mark.asyncio
async def test_create_event_success(client_with_fake_auth):
    mock_request = AsyncMock(
        return_value=_response(200, {"id": "abc123", "summary": "Appointment: Test Patient"})
    )
    original_calendar_id = settings.google_calendar_id
    settings.google_calendar_id = "test-calendar@group.calendar.google.com"
    try:
        with patch("httpx.AsyncClient.request", mock_request):
            result = await client_with_fake_auth.create_event(
                GoogleCalendarEvent(id="abc123", summary="Appointment: Test Patient")
            )
    finally:
        settings.google_calendar_id = original_calendar_id

    assert result.id == "abc123"
    call_kwargs = mock_request.call_args.kwargs
    assert call_kwargs["params"] == {"sendUpdates": "none"}
    assert "test-calendar@group.calendar.google.com" in str(mock_request.call_args.args[1])


@pytest.mark.asyncio
async def test_create_event_raises_if_calendar_id_unset(client_with_fake_auth):
    original_calendar_id = settings.google_calendar_id
    settings.google_calendar_id = None
    try:
        with pytest.raises(GoogleCalendarIntegrationError, match="GOOGLE_CALENDAR_ID"):
            await client_with_fake_auth.create_event(GoogleCalendarEvent(summary="x"))
    finally:
        settings.google_calendar_id = original_calendar_id


@pytest.mark.asyncio
async def test_create_event_non_retryable_4xx_raises_immediately(client_with_fake_auth):
    mock_request = AsyncMock(return_value=_response(400, {"error": "bad request"}))
    original_calendar_id = settings.google_calendar_id
    settings.google_calendar_id = "test-calendar@group.calendar.google.com"
    try:
        with patch("httpx.AsyncClient.request", mock_request):
            with pytest.raises(GoogleCalendarIntegrationError):
                await client_with_fake_auth.create_event(GoogleCalendarEvent(summary="x"))
    finally:
        settings.google_calendar_id = original_calendar_id
    assert mock_request.call_count == 1  # retryable=False — never retried


@pytest.mark.asyncio
async def test_delete_event_success(client_with_fake_auth):
    mock_request = AsyncMock(return_value=_response(204))
    original_calendar_id = settings.google_calendar_id
    settings.google_calendar_id = "test-calendar@group.calendar.google.com"
    try:
        with patch("httpx.AsyncClient.request", mock_request):
            await client_with_fake_auth.delete_event("evt-1")
    finally:
        settings.google_calendar_id = original_calendar_id
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_delete_event_already_gone_is_not_an_error(client_with_fake_auth):
    """404 and 410 both mean "already deleted" — a safe no-op, not a failure."""
    original_calendar_id = settings.google_calendar_id
    settings.google_calendar_id = "test-calendar@group.calendar.google.com"
    try:
        for status_code in (404, 410):
            mock_request = AsyncMock(return_value=_response(status_code))
            with patch("httpx.AsyncClient.request", mock_request):
                await client_with_fake_auth.delete_event("evt-already-gone")  # must not raise
    finally:
        settings.google_calendar_id = original_calendar_id


@pytest.mark.asyncio
async def test_delete_event_retries_on_503_then_succeeds(client_with_fake_auth):
    mock_request = AsyncMock(side_effect=[_response(503), _response(204)])
    original_calendar_id = settings.google_calendar_id
    settings.google_calendar_id = "test-calendar@group.calendar.google.com"
    try:
        with patch("httpx.AsyncClient.request", mock_request), patch("asyncio.sleep", AsyncMock()):
            await client_with_fake_auth.delete_event("evt-2")
    finally:
        settings.google_calendar_id = original_calendar_id
    assert mock_request.call_count == 2


@pytest.mark.asyncio
async def test_delete_event_exhausts_retries_and_raises(client_with_fake_auth):
    mock_request = AsyncMock(return_value=_response(503))
    original_calendar_id = settings.google_calendar_id
    settings.google_calendar_id = "test-calendar@group.calendar.google.com"
    try:
        with patch("httpx.AsyncClient.request", mock_request), patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(GoogleCalendarIntegrationError):
                await client_with_fake_auth.delete_event("evt-3")
    finally:
        settings.google_calendar_id = original_calendar_id
    assert mock_request.call_count == 4  # initial attempt + 3 retries (MAX_RETRIES)


def test_load_credentials_raises_if_unset():
    client = GoogleCalendarClient()
    original = settings.google_service_account_json
    settings.google_service_account_json = None
    try:
        with pytest.raises(GoogleCalendarIntegrationError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
            client._load_credentials()
    finally:
        settings.google_service_account_json = original


def test_load_credentials_raises_on_malformed_json():
    client = GoogleCalendarClient()
    original = settings.google_service_account_json
    settings.google_service_account_json = "{not valid json"
    try:
        with pytest.raises(GoogleCalendarIntegrationError, match="not valid JSON"):
            client._load_credentials()
    finally:
        settings.google_service_account_json = original


def test_load_credentials_lazy_not_at_construction():
    """Constructing the client must never attempt to load/parse credentials — only the first
    real call does, matching the lazy-_headers() convention used elsewhere in this codebase.
    """
    original = settings.google_service_account_json
    settings.google_service_account_json = None
    try:
        client = GoogleCalendarClient()  # must not raise
        assert client._credentials is None
    finally:
        settings.google_service_account_json = original


def _generate_fake_service_account_json() -> str:
    """A real (but throwaway, test-only, never used against any real server) RSA keypair, so
    google-auth's signer construction succeeds exactly like it would for a genuine key — avoids
    a flaky/unclear test that depends on whether a fake key shape happens to be accepted.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "test-project",
            "private_key_id": "test-key-id",
            "private_key": pem,
            "client_email": "test@test-project.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


def test_load_credentials_caches_across_calls():
    client = GoogleCalendarClient()
    original = settings.google_service_account_json
    settings.google_service_account_json = _generate_fake_service_account_json()
    try:
        first = client._load_credentials()
        second = client._load_credentials()
        assert first is second
    finally:
        settings.google_service_account_json = original
