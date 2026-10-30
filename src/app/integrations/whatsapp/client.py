"""Wraps Meta's WhatsApp Business Platform Cloud API (POST /{phone_number_id}/messages) for a
one-way, OUTBOUND-ONLY notification channel: sends a pre-approved template message to the patient
on book/reschedule/cancel. This client is the only place in the codebase that talks to Meta's
Graph API for WhatsApp. There is no inbound send of any kind — replies are handled (ignored, by
design) only by the webhook receiver in api/v1/endpoints/webhooks.py.

Auth is a plain static bearer token (a System User's permanent access token, generated once in
Meta Business Manager) — unlike Google Calendar's service-account JWT flow, there's no token
refresh to manage here, so this mirrors EdesyClient's simpler auth shape, not GoogleCalendarClient's.
"""

import asyncio

import httpx

from app.core.config import settings
from app.core.exceptions import WhatsAppIntegrationError
from app.core.logging import get_logger
from app.integrations.whatsapp.schemas import WhatsAppSendResult

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 503, 504}
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0
# Current stable Graph API version as of this integration's build (2026-10) with no expiration
# scheduled yet — bump periodically per Meta's deprecation schedule
# (developers.facebook.com/docs/graph-api/changelog/versions).
_GRAPH_API_VERSION = "v25.0"
_API_BASE_URL = f"https://graph.facebook.com/{_GRAPH_API_VERSION}"


class WhatsAppClient:
    def __init__(self) -> None:
        self._base_url = _API_BASE_URL

    def _headers(self) -> dict[str, str]:
        if not settings.meta_whatsapp_access_token:
            raise WhatsAppIntegrationError(
                "META_WHATSAPP_ACCESS_TOKEN is not configured — set it before sending WhatsApp messages"
            )
        return {
            "Authorization": f"Bearer {settings.meta_whatsapp_access_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        retryable: bool = False,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        attempt = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                attempt += 1
                try:
                    response = await client.request(method, url, json=json, headers=self._headers())
                except httpx.RequestError as exc:
                    logger.error("whatsapp_request_error", url=url, error=str(exc), attempt=attempt)
                    if not retryable or attempt > MAX_RETRIES:
                        raise WhatsAppIntegrationError(f"WhatsApp API request failed: {exc}") from exc
                    await asyncio.sleep(BASE_BACKOFF_SECONDS * attempt)
                    continue

                if response.status_code >= 400:
                    if retryable and response.status_code in RETRYABLE_STATUS_CODES and attempt <= MAX_RETRIES:
                        logger.warning(
                            "whatsapp_request_retrying",
                            url=url,
                            status_code=response.status_code,
                            attempt=attempt,
                        )
                        await asyncio.sleep(BASE_BACKOFF_SECONDS * attempt)
                        continue
                    logger.error(
                        "whatsapp_request_failed", url=url, status_code=response.status_code, body=response.text
                    )
                    raise WhatsAppIntegrationError(
                        f"WhatsApp API returned {response.status_code}: {response.text}"
                    )

                return response

    async def send_template_message(
        self, to: str, template_name: str, language_code: str, body_parameters: list[str]
    ) -> WhatsAppSendResult:
        # retryable=False — the Cloud API has no client-supplied idempotency key for a message
        # send, so a blind retry after an ambiguous network timeout risks genuinely double-
        # messaging the patient if the original request actually landed server-side. Correctness
        # instead comes from the whatsapp_message_id-already-set check in
        # services/whatsapp_notification_service.py before this is ever called.
        if not settings.meta_whatsapp_phone_number_id:
            raise WhatsAppIntegrationError("META_WHATSAPP_PHONE_NUMBER_ID is not configured")

        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": value} for value in body_parameters],
                    }
                ],
            },
        }
        logger.info("whatsapp_send_template_request", to=to, template_name=template_name)
        response = await self._request(
            "POST", f"/{settings.meta_whatsapp_phone_number_id}/messages", json=body, retryable=False
        )
        payload = response.json()
        messages = payload.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise WhatsAppIntegrationError(
                f"WhatsApp API response missing messages[0].id: {payload}"
            )
        message_id = messages[0]["id"]
        logger.info("whatsapp_send_template_response", to=to, message_id=message_id)
        return WhatsAppSendResult(message_id=message_id)


whatsapp_client = WhatsAppClient()
