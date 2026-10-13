"""Wraps Edesy's Agents, Functions/Tools, and Calls APIs (voice-agent.edesy.in).

Edesy owns the entire voice pipeline (telephony, STT, LLM, TTS, transcript, recording) — this
client is the only place in the codebase that talks to it. `POST /api/v1/calls` is documented by
Edesy as NOT idempotent, so `place_call` requires an explicit idempotency key (the caller passes
`call_schedules._id`) and only retries on 429/503/504 — never blindly retries a call placement.
"""

import asyncio

import httpx

from app.core.config import settings
from app.core.exceptions import EdesyIntegrationError
from app.core.logging import get_logger
from app.integrations.edesy.schemas import (
    EdesyAgentResponse,
    EdesyAgentUpdateRequest,
    EdesyCallSummary,
    EdesyPlaceCallRequest,
    EdesyPlaceCallResponse,
)

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 503, 504}
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0


class EdesyClient:
    def __init__(self) -> None:
        self._base_url = settings.edesy_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not settings.edesy_api_key:
            raise EdesyIntegrationError(
                "The voice agent's API key is not configured — set it before placing calls"
            )
        return {
            "Authorization": f"Bearer {settings.edesy_api_key}",
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
                    logger.error("edesy_request_error", url=url, error=str(exc), attempt=attempt)
                    if not retryable or attempt > MAX_RETRIES:
                        raise EdesyIntegrationError(f"Voice agent API request failed: {exc}") from exc
                    await asyncio.sleep(BASE_BACKOFF_SECONDS * attempt)
                    continue

                if response.status_code >= 400:
                    if retryable and response.status_code in RETRYABLE_STATUS_CODES and attempt <= MAX_RETRIES:
                        logger.warning(
                            "edesy_request_retrying",
                            url=url,
                            status_code=response.status_code,
                            attempt=attempt,
                        )
                        await asyncio.sleep(BASE_BACKOFF_SECONDS * attempt)
                        continue
                    logger.error(
                        "edesy_request_failed", url=url, status_code=response.status_code, body=response.text
                    )
                    raise EdesyIntegrationError(
                        f"Voice agent API returned {response.status_code}: {response.text}"
                    )

                return response

    async def update_agent(self, agent_id: str, payload: EdesyAgentUpdateRequest) -> EdesyAgentResponse:
        body = {k: v for k, v in payload.model_dump().items() if v is not None}
        response = await self._request("PATCH", f"/api/v1/agents/{agent_id}", json=body)
        return EdesyAgentResponse.model_validate(response.json())

    async def get_agent(self, agent_id: str) -> EdesyAgentResponse:
        response = await self._request("GET", f"/api/v1/agents/{agent_id}")
        return EdesyAgentResponse.model_validate(response.json())

    async def place_call(
        self,
        agent_id: str,
        phone_number: str,
        context: dict,
        idempotency_key: str,
        variables: dict | None = None,
        callback_url: str | None = None,
        from_number: str | None = None,
    ) -> EdesyPlaceCallResponse:
        payload = EdesyPlaceCallRequest(
            agentId=agent_id,
            phoneNumber=phone_number,
            fromNumber=from_number,
            context=context,
            variables=variables or {},
            callbackUrl=callback_url,
            idempotencyKey=idempotency_key,
        )
        request_body = payload.model_dump()
        logger.info("edesy_place_call_request", body=request_body)
        response = await self._request("POST", "/api/v1/calls", json=request_body, retryable=True)
        body = response.json()
        logger.info("edesy_place_call_response", body=body)
        result = EdesyPlaceCallResponse.from_response_body(body)
        if not result.call_id:
            logger.warning(
                "edesy_place_call_no_id_found",
                body=body,
                note="Couldn't find a call id under callId/call_id/id in the response — webhook "
                "correlation for this call will fail. Check this log's `body` and fix "
                "EdesyPlaceCallResponse.from_response_body accordingly.",
            )
        return result

    async def list_calls(self, limit: int = 50) -> list[EdesyCallSummary]:
        """The only place a call's recordingUrl has actually been observed — confirmed live
        2026-09-22. `GET /api/v1/calls/{id}` (singular) 404s for both callSid and conversationId,
        so despite the extra fetch this list is the real, working way to get one. The `callSid`
        query param appears to be silently ignored (a filtered request returned the same
        unfiltered results as an unfiltered one) — filter by callSid client-side instead.
        """
        response = await self._request("GET", f"/api/v1/calls?limit={limit}")
        body = response.json()
        calls = body.get("data", {}).get("calls", [])
        return [EdesyCallSummary.model_validate(c) for c in calls]


edesy_client = EdesyClient()
