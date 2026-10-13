"""Wraps the Google Calendar v3 API (events.insert / events.delete) for a one-way, OUTBOUND-ONLY
sync: mirrors our own book/reschedule/cancel actions onto a single pre-shared calendar, purely for
the doctor's own visibility. This client is the only place in the codebase that talks to Google
Calendar directly. There is no inbound direction of any kind — no events.list, no events.watch, no
push channel subscriptions.

Credential loading and OAuth2 JWT-bearer token refresh is delegated to google-auth
(service_account.Credentials + .refresh()) rather than hand-rolled — RSA-SHA256 signing and token
lifecycle are security-sensitive surface area a maintained library should own, unlike Edesy's
plain static-bearer-token auth which never needed us to generate or sign anything ourselves. The
actual REST calls are still plain httpx, exactly like EdesyClient.
"""

import asyncio
import json

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from app.core.config import settings
from app.core.exceptions import GoogleCalendarIntegrationError
from app.core.logging import get_logger
from app.integrations.google_calendar.schemas import GoogleCalendarEvent

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 503, 504}
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0
_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
_API_BASE_URL = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarClient:
    def __init__(self) -> None:
        self._base_url = _API_BASE_URL
        self._credentials: service_account.Credentials | None = None

    def _load_credentials(self) -> service_account.Credentials:
        if self._credentials is not None:
            return self._credentials
        if not settings.google_service_account_json:
            raise GoogleCalendarIntegrationError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is not configured — set it before calling the "
                "Google Calendar API"
            )
        try:
            info = json.loads(settings.google_service_account_json)
        except json.JSONDecodeError as exc:
            raise GoogleCalendarIntegrationError(
                f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}"
            ) from exc
        self._credentials = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
        return self._credentials

    async def _access_token(self) -> str:
        credentials = self._load_credentials()
        if not credentials.valid:
            try:
                # credentials.refresh() does one blocking HTTPS POST — run off the event loop.
                await asyncio.to_thread(credentials.refresh, GoogleAuthRequest())
            except Exception as exc:
                # google-auth raises its own RefreshError/TransportError types here — most often
                # caused by a revoked/invalid service-account key, or malformed key content.
                logger.error("google_calendar_token_refresh_failed", error=str(exc))
                raise GoogleCalendarIntegrationError(
                    f"Google Calendar auth token refresh failed: {exc}"
                ) from exc
        return credentials.token

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._access_token()}", "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        params: dict | None = None,
        retryable: bool = False,
        ignore_statuses: set[int] | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        attempt = 0
        ignore_statuses = ignore_statuses or set()

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                attempt += 1
                try:
                    response = await client.request(
                        method, url, json=json, params=params, headers=await self._headers()
                    )
                except httpx.RequestError as exc:
                    logger.error("google_calendar_request_error", url=url, error=str(exc), attempt=attempt)
                    if not retryable or attempt > MAX_RETRIES:
                        raise GoogleCalendarIntegrationError(
                            f"Google Calendar API request failed: {exc}"
                        ) from exc
                    await asyncio.sleep(BASE_BACKOFF_SECONDS * attempt)
                    continue

                if response.status_code in ignore_statuses:
                    return response

                if response.status_code >= 400:
                    if retryable and response.status_code in RETRYABLE_STATUS_CODES and attempt <= MAX_RETRIES:
                        logger.warning(
                            "google_calendar_request_retrying",
                            url=url,
                            status_code=response.status_code,
                            attempt=attempt,
                        )
                        await asyncio.sleep(BASE_BACKOFF_SECONDS * attempt)
                        continue
                    logger.error(
                        "google_calendar_request_failed", url=url, status_code=response.status_code, body=response.text
                    )
                    raise GoogleCalendarIntegrationError(
                        f"Google Calendar API returned {response.status_code}: {response.text}"
                    )

                return response

    async def create_event(self, event: GoogleCalendarEvent) -> GoogleCalendarEvent:
        # retryable=False — Calendar's events.insert has no documented idempotency guarantee
        # for a client-supplied id (Google's own docs: "we cannot guarantee that ID collisions
        # will be detected at event creation time"), so a blind retry on an ambiguous timeout
        # risks a real duplicate event if the original request actually landed server-side.
        # Correctness instead comes from the google_calendar_event_id-already-set check in
        # services/google_calendar_sync_service.py's _push_booking before this is ever called.
        if not settings.google_calendar_id:
            raise GoogleCalendarIntegrationError("GOOGLE_CALENDAR_ID is not configured")
        body = event.model_dump(exclude_none=True)
        logger.info("google_calendar_create_event_request", body=body)
        response = await self._request(
            "POST",
            f"/calendars/{settings.google_calendar_id}/events",
            json=body,
            # sendUpdates=none — explicit insurance that no Calendar notification/invite email is
            # ever sent to the patient. We also never set `attendees` at all (a bare Service
            # Account without Workspace domain-wide delegation can't reliably email attendees on
            # a calendar it doesn't own) — patient name/phone/notes go in summary/description
            # instead, for the doctor's own reading only.
            params={"sendUpdates": "none"},
            retryable=False,
        )
        result = GoogleCalendarEvent.model_validate(response.json())
        logger.info("google_calendar_create_event_response", event_id=result.id)
        return result

    async def delete_event(self, event_id: str) -> None:
        # retryable=True — deleting an already-deleted event is a safe, non-destructive no-op
        # (opposite risk profile from create).
        if not settings.google_calendar_id:
            raise GoogleCalendarIntegrationError("GOOGLE_CALENDAR_ID is not configured")
        response = await self._request(
            "DELETE",
            f"/calendars/{settings.google_calendar_id}/events/{event_id}",
            params={"sendUpdates": "none"},
            retryable=True,
            ignore_statuses={404, 410},
        )
        if response.status_code in (404, 410):
            # Already gone — manually deleted on the calendar, or a retry after a prior
            # successful delete. Treat as success, not a failure to surface.
            logger.info("google_calendar_event_already_gone", event_id=event_id, status_code=response.status_code)


google_calendar_client = GoogleCalendarClient()
