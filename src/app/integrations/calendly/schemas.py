"""Pydantic mirrors of Calendly's (api.calendly.com) request/response shapes for the Scheduling,
Webhook Subscriptions, and Users APIs this integration uses. Field names follow Calendly's own
convention (snake_case) since these cross the wire to/from their API verbatim — confirmed against
Calendly's current official docs (2026-10-08), not guessed.
"""

from typing import Any

from pydantic import BaseModel


class CalendlyLocationInput(BaseModel):
    kind: str
    location: str | None = None


class CalendlyInviteeInput(BaseModel):
    name: str | None = None
    email: str
    timezone: str
    # E.164 phone, used for Calendly's own SMS reminders — independent of which email (real or
    # placeholder) was used. See calendly_sync_service._build_invitee_input.
    text_reminder_number: str | None = None


class CalendlyQuestionAndAnswer(BaseModel):
    # `question` must exactly match one of the Event Type's own custom_questions[].name
    # (case-sensitive) — confirmed live 2026-10-09 against Calendly's real API behavior.
    question: str
    answer: str
    position: int


class CalendlyCreateInviteeRequest(BaseModel):
    event_type: str  # Event Type URI
    start_time: str  # UTC ISO8601
    invitee: CalendlyInviteeInput
    location: CalendlyLocationInput | None = None
    questions_and_answers: list[CalendlyQuestionAndAnswer] | None = None


class CalendlyInviteeResource(BaseModel):
    """A Calendly invitee resource — returned by POST /invitees, and the shape of each item under
    /scheduled_events/{uuid}/invitees. Typed loosely (passthrough extras) since this is a third-
    party API whose exact full shape hasn't been exhaustively observed — only the fields this
    integration actually reads are declared.
    """

    uri: str
    event: str  # scheduled_event URI
    email: str | None = None
    name: str | None = None
    status: str | None = None  # "active" | "canceled"
    timezone: str | None = None
    text_reminder_number: str | None = None
    # The invitee's answers to the Event Type's custom questions (confirmed live 2026-10-09 this
    # is present on both POST /invitees' response and GET .../invitees' collection items) — used
    # as a fallback phone source in calendly_sync_service._resolve_person for a direct-Calendly
    # booking whose phone was only collected via a custom question, not Calendly's native SMS-
    # reminder field (which is the only thing that populates text_reminder_number above).
    questions_and_answers: list[CalendlyQuestionAndAnswer] | None = None
    # Reschedule-pair linkage — see services/calendly_sync_service.py. On the CANCELED half of a
    # reschedule pair, `rescheduled` is true and `new_invitee` points at the replacement. On the
    # CREATED (new) half, `old_invitee` points back at the one it replaced.
    rescheduled: bool | None = None
    old_invitee: str | None = None
    new_invitee: str | None = None
    cancel_url: str | None = None
    reschedule_url: str | None = None
    cancellation: dict[str, Any] | None = None  # {"reason": ..., "canceled_by": ...} when present

    model_config = {"extra": "allow"}


class CalendlyCreateInviteeResponse(BaseModel):
    resource: CalendlyInviteeResource


class CalendlyCancelEventResponse(BaseModel):
    resource: dict[str, Any] = {}


class CalendlyScheduledEventResource(BaseModel):
    uri: str
    event_type: str | None = None  # Event Type URI this event was booked against
    start_time: str | None = None
    end_time: str | None = None
    status: str | None = None  # "active" | "canceled"

    model_config = {"extra": "allow"}


class CalendlyScheduledEventResponse(BaseModel):
    resource: CalendlyScheduledEventResource


class CalendlyUserResource(BaseModel):
    uri: str
    current_organization: str
    name: str | None = None
    email: str | None = None

    model_config = {"extra": "allow"}


class CalendlyUserResponse(BaseModel):
    resource: CalendlyUserResource


class CalendlyWebhookSubscriptionRequest(BaseModel):
    url: str
    events: list[str]
    organization: str
    scope: str = "user"
    user: str | None = None
    signing_key: str | None = None


class CalendlyWebhookSubscriptionResource(BaseModel):
    uri: str
    callback_url: str
    events: list[str] = []
    state: str | None = None  # "active" | "disabled"

    model_config = {"extra": "allow"}


class CalendlyWebhookSubscriptionResponse(BaseModel):
    resource: CalendlyWebhookSubscriptionResource


class CalendlyWebhookSubscriptionListResponse(BaseModel):
    collection: list[CalendlyWebhookSubscriptionResource] = []
