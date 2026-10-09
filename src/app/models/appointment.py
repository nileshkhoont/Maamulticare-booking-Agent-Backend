from datetime import datetime

import pymongo
from beanie import Indexed

from app.core.constants import (
    AppointmentStatus,
    BookingSource,
    CalendlySchedulingMethod,
    CalendlySyncStatus,
)
from app.db.base import TimestampedDocument


class Appointment(TimestampedDocument):
    person_id: Indexed(str)  # ref persons._id
    appointment_datetime: Indexed(datetime)  # combined date+time, stored in UTC
    duration_minutes: int | None = None
    status: AppointmentStatus
    booking_source: BookingSource
    original_appointment_id: str | None = None  # self-ref — reschedule chain
    notes: str | None = None
    created_by_call_id: str | None = None  # ref calls._id — which call resulted in this booking
    # Edesy's own call id (callSid), captured at booking time from the call-context variable
    # {{call.sid}} — NOT LLM-supplied, so it's exact and globally unique per call, even for two
    # concurrent calls with the same person. Purely an internal breadcrumb: call_service.py uses
    # it for an exact-match correlation to this appointment's real created_by_call_id once the
    # call.ended webhook creates that Call document; nothing else ever reads it.
    pending_edesy_call_id: str | None = None

    # --- Calendly sync (see services/calendly_sync_service.py) ---
    # Set once, at row-creation time, never changed afterward — the "scheduling_method = api"
    # half of the loop-prevention rule: calendly_sync_service checks this before ever creating a
    # new local row from a webhook, so a webhook that's just confirming something our own app
    # already pushed is never mistaken for a brand-new external booking.
    scheduling_method: CalendlySchedulingMethod | None = None
    # Calendly's invitee resource URI — the actual correlation key for inbound webhooks (the
    # "invitee URI already in DB" half of the loop-prevention rule). Written only after an
    # outbound push to Calendly has actually succeeded; None until then.
    calendly_invitee_uri: str | None = None
    # Calendly's scheduled-event resource URI — cancel_scheduled_event needs the uuid out of this.
    calendly_event_uri: str | None = None
    # Queue state consumed by workers/tasks/calendly_push_task.py's sweep. appointment_service.py
    # sets this to `pending` on every row it touches (create, the new row of a reschedule, and the
    # cancelled old row) — the admin-facing book/reschedule/cancel request itself never calls
    # Calendly directly, so it stays fast regardless of Calendly's latency/retries.
    calendly_sync_status: CalendlySyncStatus | None = None
    calendly_sync_error: str | None = None  # last push failure message, for admin visibility
    calendly_last_synced_at: datetime | None = None
    # True if this appointment's Calendly invitee was created with settings.calendly_placeholder_
    # email rather than the patient's own real email (Person.email was blank at push time) — set
    # at push time, not derived from the live Person record, since it describes what was true for
    # *that* historical sync, not the patient's current email state.
    is_placeholder_email: bool = False

    class Settings(TimestampedDocument.Settings):
        name = "appointments"
        indexes = [
            pymongo.IndexModel(
                [("person_id", pymongo.ASCENDING), ("appointment_datetime", pymongo.DESCENDING)]
            ),
            pymongo.IndexModel([("status", pymongo.ASCENDING)]),
            # DB-level double-booking guard: only one active (booked/rescheduled) appointment per slot.
            # Booking is confirmed NOT doctor-wise, so this is a single business-wide slot lock.
            pymongo.IndexModel(
                [("appointment_datetime", pymongo.ASCENDING)],
                unique=True,
                partialFilterExpression={"status": {"$in": ["booked", "rescheduled"]}},
            ),
            # Webhook correlation lookup (appointment_repository.get_by_calendly_invitee_uri) —
            # plain equality match, same pattern as Call.edesy_call_id's sparse index.
            pymongo.IndexModel([("calendly_invitee_uri", pymongo.ASCENDING)], sparse=True),
            # Speeds up calendly_push_task's sweep query for pending/failed rows.
            pymongo.IndexModel([("calendly_sync_status", pymongo.ASCENDING)], sparse=True),
        ]
