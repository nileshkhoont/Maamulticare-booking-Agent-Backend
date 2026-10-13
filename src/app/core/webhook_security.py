import hashlib
import hmac

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
