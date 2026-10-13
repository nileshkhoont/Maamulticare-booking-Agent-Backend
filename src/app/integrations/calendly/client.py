"""Wraps Calendly's Scheduling, Webhook Subscriptions, and Users APIs (api.calendly.com). This
client is the only place in the codebase that talks to Calendly directly.

Confirmed live against Calendly's own current docs (2026-10-08), not guessed:
- POST /invitees (create a booking) and POST /scheduled_events/{uuid}/cancellation both work on
  Calendly's free plan.
- POST /webhook_subscriptions requires a paid Standard-or-higher plan — this account is currently
  on the free plan, so webhook_subscription calls will fail until that upgrade happens. That's
  expected, not a bug in this client.
- There is no reschedule endpoint. Calendly itself implements a reschedule as cancelling the old
  scheduled event and creating a new invitee — see services/calendly_sync_service.py for how this
  client's two separate calls are sequenced to match.
"""

import asyncio

import httpx

from app.core.config import settings
from app.core.exceptions import CalendlyIntegrationError
from app.core.logging import get_logger
from app.integrations.calendly.schemas import (
    CalendlyCancelEventResponse,
    CalendlyCreateInviteeRequest,
    CalendlyCreateInviteeResponse,
    CalendlyInviteeResource,
    CalendlyScheduledEventResource,
    CalendlyScheduledEventResponse,
    CalendlyUserResponse,
    CalendlyWebhookSubscriptionListResponse,
    CalendlyWebhookSubscriptionRequest,
    CalendlyWebhookSubscriptionResponse,
)

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 503, 504}
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0


class CalendlyClient:
    def __init__(self) -> None:
        self._base_url = settings.calendly_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not settings.calendly_pat:
            raise CalendlyIntegrationError(
                "CALENDLY_PAT is not configured — set it before calling the Calendly API"
            )
        return {
            "Authorization": f"Bearer {settings.calendly_pat}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        params: dict | None = None,
        retryable: bool = False,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        attempt = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                attempt += 1
                try:
                    response = await client.request(
                        method, url, json=json, params=params, headers=self._headers()
                    )
                except httpx.RequestError as exc:
                    logger.error("calendly_request_error", url=url, error=str(exc), attempt=attempt)
                    if not retryable or attempt > MAX_RETRIES:
                        raise CalendlyIntegrationError(f"Calendly API request failed: {exc}") from exc
                    await asyncio.sleep(BASE_BACKOFF_SECONDS * attempt)
                    continue

                if response.status_code >= 400:
                    if retryable and response.status_code in RETRYABLE_STATUS_CODES and attempt <= MAX_RETRIES:
                        logger.warning(
                            "calendly_request_retrying",
                            url=url,
                            status_code=response.status_code,
                            attempt=attempt,
                        )
                        await asyncio.sleep(BASE_BACKOFF_SECONDS * attempt)
                        continue
                    logger.error(
                        "calendly_request_failed", url=url, status_code=response.status_code, body=response.text
                    )
                    raise CalendlyIntegrationError(
                        f"Calendly API returned {response.status_code}: {response.text}"
                    )

                return response

    async def create_invitee(self, payload: CalendlyCreateInviteeRequest) -> CalendlyCreateInviteeResponse:
        # retryable=False — Calendly's invitee creation has no documented idempotency key, so a
        # blind retry on a 503/504 risks a duplicate booking if the original request actually
        # landed server-side. A failed create is instead left `calendly_sync_status=failed` for
        # the reconciliation sweep to safely retry later, since by then it can check whether the
        # appointment already ended up with a calendly_invitee_uri before trying again.
        body = payload.model_dump(exclude_none=True)
        logger.info("calendly_create_invitee_request", body=body)
        response = await self._request("POST", "/invitees", json=body, retryable=False)
        result = CalendlyCreateInviteeResponse.model_validate(response.json())
        logger.info("calendly_create_invitee_response", invitee_uri=result.resource.uri, event_uri=result.resource.event)
        return result

    async def cancel_scheduled_event(
        self, event_uuid: str, reason: str | None = None
    ) -> CalendlyCancelEventResponse:
        # retryable=True — cancelling an already-cancelled event is a safe, non-destructive no-op
        # on Calendly's side (opposite risk profile from create), so retrying 429/503/504 is safe.
        body = {"reason": reason} if reason else {}
        response = await self._request(
            "POST", f"/scheduled_events/{event_uuid}/cancellation", json=body, retryable=True
        )
        return CalendlyCancelEventResponse.model_validate(response.json())

    async def get_scheduled_event(self, event_uuid: str) -> CalendlyScheduledEventResponse:
        response = await self._request("GET", f"/scheduled_events/{event_uuid}", retryable=True)
        return CalendlyScheduledEventResponse.model_validate(response.json())

    async def get_current_user(self) -> CalendlyUserResponse:
        response = await self._request("GET", "/users/me", retryable=True)
        return CalendlyUserResponse.model_validate(response.json())

    async def list_webhook_subscriptions(
        self, organization: str, scope: str = "user", user: str | None = None
    ) -> CalendlyWebhookSubscriptionListResponse:
        params = {"organization": organization, "scope": scope}
        if user:
            params["user"] = user
        response = await self._request("GET", "/webhook_subscriptions", params=params, retryable=True)
        return CalendlyWebhookSubscriptionListResponse.model_validate(response.json())

    async def create_webhook_subscription(
        self, payload: CalendlyWebhookSubscriptionRequest
    ) -> CalendlyWebhookSubscriptionResponse:
        # retryable=False — creating a webhook subscription isn't idempotent (a blind retry could
        # create a second, duplicate subscription that double-delivers every future event).
        body = payload.model_dump(exclude_none=True)
        response = await self._request("POST", "/webhook_subscriptions", json=body, retryable=False)
        return CalendlyWebhookSubscriptionResponse.model_validate(response.json())

    async def list_scheduled_events(
        self,
        user: str,
        min_start_time: str,
        max_start_time: str,
        count: int = 100,
        status: str | None = None,
    ) -> list[CalendlyScheduledEventResource]:
        """Used only by the reconciliation sweep (workers/tasks/calendly_reconciliation_task.py) —
        a GET-based substitute for real-time webhooks while the account is on the free plan.
        Follows Calendly's own pagination (`pagination.next_page`, a full URL or null) until
        exhausted — verify the exact field name against a real response during manual testing,
        since this hasn't been directly observed yet (only documented via search, not fetched).

        `status` ("active" | "canceled"): unset returns Calendly's own default, which is not
        confirmed to include cancelled events — the reconciliation task calls this once per status
        value to be safe, rather than assume one call surfaces both.
        """
        events: list[CalendlyScheduledEventResource] = []
        params: dict = {
            "user": user,
            "min_start_time": min_start_time,
            "max_start_time": max_start_time,
            "count": count,
        }
        if status:
            params["status"] = status
        next_url: str | None = None
        while True:
            if next_url:
                response = await self._request("GET", next_url, retryable=True)
            else:
                response = await self._request("GET", "/scheduled_events", params=params, retryable=True)
            body = response.json()
            events.extend(CalendlyScheduledEventResource.model_validate(e) for e in body.get("collection", []))
            next_url = (body.get("pagination") or {}).get("next_page")
            if not next_url:
                break
        return events

    async def get_event_invitees(self, event_uuid: str) -> list[CalendlyInviteeResource]:
        """Used only by the reconciliation sweep — the invitee(s) for one scheduled event, same
        resource shape POST /invitees returns."""
        response = await self._request(
            "GET", f"/scheduled_events/{event_uuid}/invitees", retryable=True
        )
        body = response.json()
        return [CalendlyInviteeResource.model_validate(i) for i in body.get("collection", [])]


calendly_client = CalendlyClient()
