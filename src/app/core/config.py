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

    # Google Calendar — a one-way, OUTBOUND-ONLY mirror of appointments onto a single dedicated
    # calendar, purely for the doctor's own visibility (see services/google_calendar_sync_service.py).
    # Patients can NEVER book/reschedule/cancel through Google Calendar directly — only via the
    # Edesy AI phone system above. There is no inbound sync, no webhooks, and no "watch" channel
    # subscriptions of any kind.
    #
    # Auth is a Google Cloud SERVICE ACCOUNT (not OAuth), granted access via plain calendar sharing
    # — confirmed to work with a 100% free personal Gmail account; no Google Workspace and no
    # domain-wide delegation needed (that's a separate, Workspace-only mechanism this integration
    # does not use). Setup: create a GCP project -> enable the Calendar API -> create a Service
    # Account -> download its JSON key -> in the doctor's own Gmail, create a NEW secondary
    # calendar (Settings -> "Create new calendar", NOT the primary one) -> share that calendar
    # with the service account's own email (the JSON key's "client_email" field), permission
    # "Make changes to events".
    google_service_account_json: str | None = None
    # The dedicated secondary calendar's own ID (NOT the doctor's "primary" calendar) — found on
    # its own Settings page under "Integrate calendar", looks like
    # "xxxxxxxxxxxxxxxxxxxxxxxxxxxx@group.calendar.google.com".
    google_calendar_id: str | None = None
    # Plain text shown in each event's `location` field (e.g. the hospital's physical address).
    # Optional — a single free-text string, with no "kind" concept to configure (unlike a
    # multi-location-type scheduling product), since Calendar's `location` field is just text.
    google_calendar_event_location: str | None = None
    # Fallback event duration when Appointment.duration_minutes wasn't specified at booking time
    # — the event's end time becomes start + this many minutes.
    google_calendar_default_event_duration_minutes: int = 30
    # How often workers/tasks/google_calendar_push_task.py's sweep looks for Appointments with
    # google_calendar_sync_status=pending to push — mirrors outbound_call_poll_interval_seconds's
    # role for the (unrelated) outbound-call queue. Poll-based, not fired inline from
    # book/reschedule/cancel, matching this codebase's existing convention of never calling an
    # external integration synchronously from an admin-facing request.
    google_calendar_push_poll_interval_seconds: int = 30


settings = Settings()
