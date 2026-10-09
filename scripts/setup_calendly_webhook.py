"""Standalone script: creates the Calendly webhook subscription that /api/v1/webhooks/calendly
needs, so changes made directly in Calendly (a doctor-sent invite link, a patient self-service
reschedule/cancel via Calendly's own emails) sync back into this system in real time.

NOT run automatically at startup, unlike db/init_db.py's schema validators — a webhook
subscription create is a mutating call against a third-party, PLAN-GATED API (webhooks require
Calendly Standard or higher; confirmed live against Calendly's docs 2026-10-08) that would hard-
fail on every single app restart while the account is still on the free plan. This is a one-off,
manually-run step instead, after upgrading.

Resolves the organization/user URI live via GET /users/me rather than requiring them as static
config — one less thing to keep in sync. Idempotent: lists existing subscriptions first and skips
if one already matches this deployment's callback URL + events, so it's safe to re-run.

Usage: python scripts/setup_calendly_webhook.py
Requires CALENDLY_PAT and PUBLIC_BASE_URL already set in .env. On success, prints the signing key
to paste into CALENDLY_WEBHOOK_SIGNING_KEY — until that's set, /api/v1/webhooks/calendly 401s
every delivery.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.core.config import settings  # noqa: E402
from app.core.exceptions import CalendlyIntegrationError  # noqa: E402
from app.integrations.calendly.client import calendly_client  # noqa: E402
from app.integrations.calendly.schemas import CalendlyWebhookSubscriptionRequest  # noqa: E402

EVENTS = ["invitee.created", "invitee.canceled"]


async def main() -> None:
    if not settings.calendly_pat:
        print("CALENDLY_PAT is not set in .env — nothing to do.")
        return

    callback_url = f"{settings.public_base_url.rstrip('/')}/api/v1/webhooks/calendly"

    try:
        user = await calendly_client.get_current_user()
    except CalendlyIntegrationError as exc:
        print(f"Could not reach Calendly's API: {exc}")
        return

    organization = user.resource.current_organization
    user_uri = user.resource.uri

    existing = await calendly_client.list_webhook_subscriptions(organization=organization, scope="user")
    for subscription in existing.collection:
        if subscription.callback_url == callback_url and set(subscription.events) >= set(EVENTS):
            print(
                f"A matching webhook subscription already exists ({subscription.uri}, "
                f"state={subscription.state}) — nothing to do. If you've lost the signing key, "
                "there's no way to retrieve it after creation; delete this subscription in the "
                "Calendly dashboard and re-run this script to get a new one."
            )
            return

    try:
        response = await calendly_client.create_webhook_subscription(
            CalendlyWebhookSubscriptionRequest(
                url=callback_url,
                events=EVENTS,
                organization=organization,
                scope="user",
                user=user_uri,
            )
        )
    except CalendlyIntegrationError as exc:
        print(
            f"Could not create the webhook subscription: {exc}\n"
            "If this says the account isn't on a plan that supports webhooks, that's expected "
            "until the Standard-plan upgrade happens — this script is meant to be re-run after."
        )
        return

    # signing_key isn't a declared field on CalendlyWebhookSubscriptionResource (its exact
    # presence/shape on the create response wasn't directly observed during design — only
    # documented via search, not a live fetch) — model_extra picks it up regardless, since the
    # schema allows extra fields. Falls back to the whole raw resource if the key isn't found
    # under the name we expect, so this is never a dead end even if the field name differs.
    signing_key = (response.resource.model_extra or {}).get("signing_key")

    print(f"Created webhook subscription {response.resource.uri} for {callback_url}, events={response.resource.events}.\n")
    if signing_key:
        print(f"IMPORTANT — copy this into .env now, it's shown only once:\nCALENDLY_WEBHOOK_SIGNING_KEY={signing_key}")
    else:
        print(
            "Could not find a `signing_key` field on the response — here's the full raw resource "
            f"so you can find it under whatever it's actually called:\n{response.resource.model_dump()}\n"
            "Copy whatever the real key is into .env as CALENDLY_WEBHOOK_SIGNING_KEY=<key> — "
            "Calendly only shows it once, at creation time."
        )


if __name__ == "__main__":
    asyncio.run(main())
