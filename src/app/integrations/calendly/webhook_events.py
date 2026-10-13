"""Typed parsing for Calendly's signed webhook events. Verify the signature with
core/webhook_security.py's verify_calendly_signature BEFORE parsing the body with these.

Confirmed against Calendly's current official docs (2026-10-08) — the envelope is:

    {
      "event": "invitee.created",
      "created_at": "2026-10-08T10:00:00.000000Z",
      "payload": {
        "uri": "https://api.calendly.com/scheduled_events/.../invitees/...",
        "event": "https://api.calendly.com/scheduled_events/...",
        "email": "patient@example.com",
        "name": "Jordan Lee",
        "status": "active",
        "timezone": "Asia/Kolkata",
        "text_reminder_number": "+919876543210",
        "rescheduled": false,
        "old_invitee": null,
        "new_invitee": null,
        "cancel_url": "...",
        "reschedule_url": "...",
        "cancellation": null
      }
    }

Only two events are subscribed to (see scripts/setup_calendly_webhook.py): `invitee.created` and
`invitee.canceled`. A reschedule is NOT its own event — Calendly fires BOTH of the above for a
reschedule: `invitee.canceled` on the old invitee (with `rescheduled: true` and `new_invitee` set),
and `invitee.created` on the new one (with `old_invitee` pointing back). Delivery order between
the two is not guaranteed — services/calendly_sync_service.py is written to handle either arriving
first.

Same resilience convention as integrations/edesy/webhook_events.py: every field is optional except
what's structurally required to do anything useful, and `parse_webhook_event` never raises — it
logs and returns None on anything unrecognized, so the receiving endpoint can always log-and-accept
(200) rather than fail the delivery.
"""

from typing import Any

from pydantic import BaseModel

from app.core.logging import get_logger

logger = get_logger(__name__)

RECOGNIZED_EVENTS = {"invitee.created", "invitee.canceled"}


class CalendlyWebhookInviteePayload(BaseModel):
    uri: str
    event: str  # scheduled_event URI
    email: str | None = None
    name: str | None = None
    status: str | None = None
    timezone: str | None = None
    text_reminder_number: str | None = None
    rescheduled: bool | None = None
    old_invitee: str | None = None
    new_invitee: str | None = None
    cancellation: dict[str, Any] | None = None  # {"reason": ..., "canceled_by": ...}

    model_config = {"extra": "allow"}


class CalendlyWebhookEvent(BaseModel):
    event: str  # "invitee.created" | "invitee.canceled"
    created_at: str | None = None
    payload: CalendlyWebhookInviteePayload


def parse_webhook_event(payload: dict[str, Any]) -> CalendlyWebhookEvent | None:
    """Returns a parsed CalendlyWebhookEvent, or None if the payload doesn't match (unrecognized
    event name, or Calendly changed the shape) — callers should log-and-accept (200) rather than
    treat None as a hard failure, same convention as the Edesy webhook handler.
    """
    event_name = payload.get("event")
    if event_name not in RECOGNIZED_EVENTS:
        logger.warning("calendly_webhook_unrecognized_event", event_name=event_name, payload=payload)
        return None

    try:
        return CalendlyWebhookEvent.model_validate(payload)
    except Exception:
        logger.exception("calendly_webhook_shape_mismatch", payload=payload)
        return None
