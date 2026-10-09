from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    port: int = 5000
    environment: str = "development"
    # Our own app logs at this level; noisy third-party libraries (pymongo, httpx, the --reload
    # file watcher, etc.) are always capped at WARNING regardless of this — see core/logging.py.
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    # Dev-only: lets the frontend be reached from another device on the same LAN (e.g. testing
    # on a phone via http://192.168.x.x:3000) without hardcoding that machine's IP into
    # cors_origins above — which would break again the next time DHCP hands out a different one.
    # Matches http://<private-network IP>:<any port> only; never applied outside development
    # (see register_middleware in core/middleware.py), so production CORS stays the explicit
    # cors_origins list above.
    cors_origin_regex_dev: str = (
        r"^http://(localhost|127\.0\.0\.1"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}):\d+$"
    )
    # Publicly reachable base URL for THIS backend — Edesy's servers call agent_tools/webhooks
    # endpoints here, so in any real deployment this must be a public HTTPS URL, not localhost.
    public_base_url: str = "http://localhost:5000"

    # MongoDB
    mongodb_uri: str
    mongodb_db_name: str = "ai_calling_agent"

    # Admin JWT auth
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_minutes: int = 60 * 24 * 7

    # Edesy AI Voice Agent (voice-agent.edesy.in)
    edesy_base_url: str = "https://voice-agent.edesy.in"
    edesy_api_key: str | None = None
    edesy_webhook_secret: str | None = None
    edesy_agent_id: str | None = None
    # Caller ID (E.164, e.g. "+919876543210") for every outbound call — sent as Edesy's own
    # `fromNumber` field on POST /api/v1/calls. Optional: when unset, outbound_call_task.py omits
    # it and Edesy falls back to its own account-level default number, so a missing value never
    # blocks calls from going out — it's a graceful fallback, not a hard requirement. Strongly
    # recommended to set it anyway: leaving it unset was root-caused (2026-09-30) to Edesy silently
    # placing outbound calls from the free trial number shown on the Phone Numbers dashboard page —
    # NOT the hospital's actual purchased number — despite that purchased number being marked
    # "default" elsewhere in the same dashboard. Setting this explicitly is what makes the calling
    # number deterministic and under our control instead of depending on Edesy's own
    # (observed-unreliable) default resolution.
    edesy_from_number: str | None = None

    # Shared secret Edesy's agent_tools calls must present (not an admin JWT)
    agent_tool_secret: str

    # Celery / Redis
    redis_url: str = "redis://127.0.0.1:6379/0"
    celery_broker_url: str = "redis://127.0.0.1:6379/0"
    celery_result_backend: str = "redis://127.0.0.1:6379/1"

    # call_schedules queue worker
    outbound_call_poll_interval_seconds: int = 30

    # Dev-only fallback: runs the same outbound-dispatch/stuck-sweep logic on a timer inside the
    # API process itself, instead of via Celery+Redis. OFF by default — Celery+Redis (workers/) is
    # the documented, production-intended path; this exists purely so the queue still works when
    # Redis isn't available (e.g. local Windows dev without Docker/WSL set up).
    enable_inprocess_scheduler: bool = False

    # Calendly (api.calendly.com) — mirrors appointments booked/rescheduled/cancelled in our own
    # system onto Dr. Shyani's Calendly calendar, and (once CALENDLY_WEBHOOK_SIGNING_KEY is set,
    # which requires upgrading off Calendly's free plan — webhooks are a paid-plan-only feature,
    # confirmed against Calendly's own docs 2026-10-08) syncs changes made directly in Calendly
    # back into ours. See services/calendly_sync_service.py.
    calendly_base_url: str = "https://api.calendly.com"
    # Personal Access Token — officially Calendly's recommended auth for exactly this case (one
    # backend integration acting on one organization's own account), not OAuth: no multi-tenant
    # "connect any user's Calendly" flow is needed here, so there's no client id/secret/redirect
    # URI to manage. Generated from Calendly's own Integrations & Apps page.
    calendly_pat: str | None = None
    # The Calendly Event Type to book every appointment against (its own URI, e.g.
    # "https://api.calendly.com/event_types/AAAAAAAAAAAAAAAA") — a single fixed consultation-type
    # event, not chosen per-appointment. Required before any outbound push can actually succeed.
    calendly_event_type_uri: str | None = None
    # The above Event Type's configured location "kind" (e.g. "physical", "google_conference",
    # "phone_call") — Calendly's POST /invitees REQUIRES a matching `location` object whenever the
    # Event Type specifies one, and REJECTS it entirely when the Event Type specifies none, so this
    # must mirror whatever's actually configured on CALENDLY_EVENT_TYPE_URI in the Calendly
    # dashboard (Event Types -> this event -> Location). Left unset, _push_booking sends no
    # location at all — only correct if the Event Type itself has none configured.
    calendly_event_location_kind: str | None = None
    # Free-text accompanying the kind above — e.g. the clinic's physical address for "physical",
    # or a dial-in number for "phone_call". Not needed for "google_conference" (Calendly
    # auto-generates the meeting link). Ignored if calendly_event_location_kind is unset.
    calendly_event_location_text: str | None = None
    # HMAC-SHA256 key for verifying the `Calendly-Webhook-Signature` header on inbound deliveries
    # at /api/v1/webhooks/calendly — printed by scripts/setup_calendly_webhook.py when the webhook
    # subscription is created (a one-off, manually-run step, NOT automatic at startup — see that
    # script's own docstring for why). Left unset until the account is upgraded to a plan that
    # supports webhooks at all; the route 401s everything until this is configured, same as how
    # EDESY_WEBHOOK_SECRET unset would behave.
    calendly_webhook_signing_key: str | None = None
    # Replay-attack tolerance window for a webhook's `t=` timestamp, per Calendly's own documented
    # recommendation (~3 minutes).
    calendly_webhook_tolerance_seconds: int = 180
    # Calendly requires an invitee email on every booking, but patients here are identified by
    # phone and usually have no email on file yet. This fixed address is used whenever a patient's
    # own Person.email is blank, so every appointment still syncs to Calendly regardless — see
    # Appointment.is_placeholder_email, which tracks which bookings used it so this is easy to
    # migrate off once staff start collecting real emails.
    calendly_placeholder_email: str = "harshjagani@movya.com"
    # How often workers/tasks/calendly_push_task.py's sweep looks for Appointments with
    # calendly_sync_status=pending to push — mirrors outbound_call_poll_interval_seconds's role
    # for the (unrelated) outbound-call queue. A push is poll-based, not fired inline from the
    # book/reschedule/cancel request itself, matching this codebase's existing convention of
    # never calling an external integration synchronously from an admin-facing request.
    calendly_push_poll_interval_seconds: int = 30


settings = Settings()
