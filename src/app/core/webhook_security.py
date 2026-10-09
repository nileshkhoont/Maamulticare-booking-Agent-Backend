import hashlib
import hmac
import time

from app.core.config import settings


def _constant_time_secret_match(candidate: str | None) -> bool:
    if not candidate or not settings.edesy_webhook_secret:
        return False
    return hmac.compare_digest(candidate, settings.edesy_webhook_secret)


def verify_static_header_secret(header_value: str | None) -> bool:
    """Verifies a static shared-secret header (e.g. `Authorization: Bearer <secret>` or
    `X-Webhook-Secret: <secret>`) — the auth mechanism the Vani/Edesy dashboard's webhook panel
    actually exposes (a free-form "Custom Headers" field, not a documented HMAC signing secret).
    Accepts either the raw secret or a "Bearer <secret>" value.
    """
    if not header_value:
        return False
    candidate = header_value.strip()
    if candidate.lower().startswith("bearer "):
        candidate = candidate[7:].strip()
    return _constant_time_secret_match(candidate)


def verify_edesy_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verifies an HMAC-SHA256 `X-Webhook-Signature` header, kept as a fallback in case a
    provider account IS configured for signed webhooks (some Edesy/Vani plans may differ) —
    tried only if verify_static_header_secret doesn't match.
    """
    if not signature_header or not settings.edesy_webhook_secret:
        return False

    expected = hmac.new(
        settings.edesy_webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    candidate = signature_header.strip()
    if "=" in candidate:
        candidate = candidate.split("=", 1)[1]

    return hmac.compare_digest(expected, candidate)


def verify_calendly_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Verifies Calendly's `Calendly-Webhook-Signature` header, formatted `t=<unix ts>,v1=<hex
    sig>` — confirmed against Calendly's own current docs (2026-10-08). The signed payload is
    `f"{t}.{raw_body}"`, HMAC-SHA256'd with the signing key Calendly hands back when the
    subscription is created (scripts/setup_calendly_webhook.py). Also rejects a stale timestamp
    (replay-attack protection) — Calendly's own docs recommend a ~3 minute tolerance window,
    configurable here via CALENDLY_WEBHOOK_TOLERANCE_SECONDS.
    """
    if not signature_header or not settings.calendly_webhook_signing_key:
        return False

    parts: dict[str, str] = {}
    for chunk in signature_header.split(","):
        if "=" not in chunk:
            continue
        key, _, value = chunk.strip().partition("=")
        parts[key] = value

    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        return False

    try:
        if abs(time.time() - int(timestamp)) > settings.calendly_webhook_tolerance_seconds:
            return False
    except ValueError:
        return False

    signed_payload = f"{timestamp}.{raw_body.decode('utf-8')}".encode()
    expected = hmac.new(
        settings.calendly_webhook_signing_key.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)
