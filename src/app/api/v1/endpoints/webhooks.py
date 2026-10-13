from fastapi import APIRouter, HTTPException, Request, status

from app.core.logging import get_logger
from app.core.webhook_security import verify_edesy_signature, verify_static_header_secret
from app.integrations.edesy.webhook_events import parse_webhook_event
from app.schemas.common import Message
from app.services.call_service import call_service

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = get_logger(__name__)


@router.post("/edesy", response_model=Message)
async def edesy_webhook(request: Request) -> Message:
    """Receives Vani/Edesy's webhook deliveries. Per the dashboard's own description ("After
    every call ends"), there is exactly one real event — `call.ended`, fired once per call with
    the full outcome + transcript (see integrations/edesy/webhook_events.py for how this was
    confirmed against a real captured payload, and why the shape is nested rather than flat).

    On anything we can't parse (unrecognized event name, or Vani changing the shape again), we
    log the full payload and still return 200 — this is an undocumented third-party API, and a
    shape drift here must show up in our own logs, not as a "delivery failed" state on Vani's
    dashboard that could cause them to disable or back off the webhook.
    """
    raw_body = await request.body()

    # The dashboard's webhook panel only exposes a URL + free-form "Custom Headers" field (no
    # documented HMAC signing secret), so a static shared-secret header is the primary check —
    # configure one of these in the dashboard with EDESY_WEBHOOK_SECRET as the value. The
    # X-Webhook-Signature/HMAC path is kept as a fallback in case the account does sign requests.
    authorized = (
        verify_static_header_secret(request.headers.get("Authorization"))
        or verify_static_header_secret(request.headers.get("X-Webhook-Secret"))
        or verify_edesy_signature(raw_body, request.headers.get("X-Webhook-Signature"))
    )
    if not authorized:
        logger.warning("edesy_webhook_unauthorized")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook credentials")

    try:
        payload = await request.json()
    except ValueError:
        logger.warning("edesy_webhook_invalid_json", body=raw_body[:2000])
        return Message(detail="ok")

    event = parse_webhook_event(payload)
    if event is None:
        # Already logged (with the full payload) inside parse_webhook_event.
        return Message(detail="ok")

    await call_service.handle_call_ended(event)
    return Message(detail="ok")
