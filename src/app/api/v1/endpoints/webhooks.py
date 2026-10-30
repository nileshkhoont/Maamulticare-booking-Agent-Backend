from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.config import settings
from app.core.logging import get_logger
from app.core.webhook_security import (
    verify_edesy_signature,
    verify_meta_signature,
    verify_static_header_secret,
)
from app.integrations.edesy.webhook_events import parse_webhook_event
from app.schemas.common import Message
from app.services.call_service import call_service
from app.services.whatsapp_notification_service import whatsapp_notification_service

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


@router.get("/whatsapp")
async def whatsapp_webhook_verify(request: Request) -> Response:
    """Meta's one-time GET verification handshake (App Dashboard > WhatsApp > Configuration >
    Webhook > Verify and Save): echo back `hub.challenge` as plain text if `hub.mode` is
    "subscribe" and `hub.verify_token` matches our own configured value, else reject.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if (
        mode == "subscribe"
        and token is not None
        and settings.meta_whatsapp_webhook_verify_token is not None
        and token == settings.meta_whatsapp_webhook_verify_token
        and challenge is not None
    ):
        return Response(content=challenge, media_type="text/plain")

    logger.warning("whatsapp_webhook_verify_rejected", mode=mode)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed")


@router.post("/whatsapp", response_model=Message)
async def whatsapp_webhook(request: Request) -> Message:
    """Receives Meta's WhatsApp webhook deliveries — in practice, only message status updates
    (sent/delivered/read/failed) for messages this system sent, since the single `messages` field
    subscription carries both those and inbound patient replies. Inbound replies (a `messages[]`
    entry) are deliberately logged and ignored — this is a one-way, OUTBOUND-ONLY notification
    channel, not a chatbot; any real reply belongs in the clinic's own WhatsApp Business app, not
    in this system. See services/whatsapp_notification_service.py's module docstring.

    On anything we can't parse, we log and still return 200 — Meta disables/backs off a webhook
    URL that looks like it's failing deliveries, and a shape drift here must show up in our own
    logs instead.
    """
    raw_body = await request.body()

    if not verify_meta_signature(raw_body, request.headers.get("X-Hub-Signature-256")):
        logger.warning("whatsapp_webhook_unauthorized")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    try:
        payload = await request.json()
    except ValueError:
        logger.warning("whatsapp_webhook_invalid_json", body=raw_body[:2000])
        return Message(detail="ok")

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            for message in value.get("messages", []):
                # Deliberately not processed — see this function's docstring.
                logger.info("whatsapp_inbound_message_ignored", from_=message.get("from"))

            for status_event in value.get("statuses", []):
                message_id = status_event.get("id")
                event_status = status_event.get("status")
                if not message_id or not event_status:
                    logger.warning("whatsapp_status_event_malformed", status_event=status_event)
                    continue
                error_detail = None
                errors = status_event.get("errors") or []
                if errors:
                    error_detail = errors[0].get("title") or errors[0].get("message")
                await whatsapp_notification_service.process_status_webhook_event(
                    message_id=message_id, new_status=event_status, error_detail=error_detail
                )

    return Message(detail="ok")
