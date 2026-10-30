"""Pydantic mirrors of Edesy's (voice-agent.edesy.in) request/response shapes for the Agents,
Functions/Tools, and Calls APIs this integration uses. Field names follow Edesy's own
camelCase convention since these cross the wire to/from their API verbatim.
"""

from typing import Any

from pydantic import BaseModel


class EdesyAgentUpdateRequest(BaseModel):
    prompt: str | None = None
    greetingMessage: str | None = None
    language: str | None = None
    voice: str | None = None
    llmProvider: str | None = None


class EdesyAgentResponse(BaseModel):
    id: str
    name: str
    prompt: str
    greetingMessage: str
    language: str
    voice: str | None = None
    llmProvider: str | None = None


class EdesyPlaceCallRequest(BaseModel):
    agentId: str
    phoneNumber: str
    # Caller ID this call is placed FROM (E.164) — confirmed via Edesy's own docs
    # (edesy.in/docs/voice-agent/api/trigger-call) as "Caller ID / from number. If not provided,
    # uses the default number for your workspace." Always sent (see Settings.edesy_from_number's
    # docstring) rather than omitted, since that documented fallback was observed live to resolve
    # to the wrong (trial) number.
    fromNumber: str | None = None
    context: dict[str, Any] = {}
    # Dashboard-level {variable} prompt substitution (Agent Instructions -> Variables). Field
    # name confirmed correct against Edesy's own API docs (trigger-call) — but the PROMPT TEXT
    # must reference these with single braces ({person_name}), not double ({{person_name}}); see
    # prompts/call_variables.py's module docstring for the 2026-10-16 real-call confirmation.
    # `context` above is kept too since it was already being sent before this was added.
    variables: dict[str, Any] = {}
    callbackUrl: str | None = None
    idempotencyKey: str | None = None


class EdesyPlaceCallResponse(BaseModel):
    """The real Vani/Edesy response wraps the payload as
    {"success": bool, "data": {"callSid": ..., "conversationId": ..., "status": ...}} — confirmed
    live 2026-09-16 against real test calls, and different from the flat shape originally guessed
    from the folder-structure doc. `raw` keeps the full response regardless, since other fields
    (e.g. whatever conveys per-provider call state) may turn out to matter later.
    """

    success: bool = True
    call_id: str | None = None
    conversation_id: str | None = None
    status: str | None = None
    raw: dict[str, Any] = {}

    @classmethod
    def from_response_body(cls, body: dict[str, Any]) -> "EdesyPlaceCallResponse":
        data = body.get("data", body) if isinstance(body.get("data"), dict) else body
        return cls(
            success=body.get("success", True),
            # Confirmed live 2026-09-16: the real key is `callSid`, not `callId`/`call_id`/`id`.
            call_id=data.get("callSid") or data.get("callId") or data.get("call_id") or data.get("id"),
            conversation_id=data.get("conversationId") or data.get("conversation_id"),
            status=data.get("status"),
            raw=body,
        )


class EdesyCallSummary(BaseModel):
    """One entry from `GET /api/v1/calls` — confirmed live 2026-09-22 (the shape
    EdesyCallResponse/get_call guessed at was wrong: `GET /api/v1/calls/{id}` 404s for both
    callSid and conversationId; the list endpoint below is the only place a recording has
    actually been observed). recordingUrl is a Cloudflare-served link keyed by conversationId,
    not callSid — `Cache-Control: max-age=31536000, immutable` on a real fetch suggests it's
    durable, not a short-lived signed URL, but that's an observation, not a guarantee from Edesy.
    """

    conversationId: str
    callSid: str
    agentId: int | str | None = None
    agentName: str | None = None
    phoneNumber: str | None = None
    status: str | None = None
    duration: int | None = None
    recordingUrl: str | None = None
    source: str | None = None
    startTime: str | None = None
    endTime: str | None = None


class EdesyCallListResponse(BaseModel):
    calls: list[EdesyCallSummary] = []
    total: int | None = None
