from enum import Enum


class AdminRole(str, Enum):
    super_admin = "super_admin"
    admin = "admin"
    viewer = "viewer"


class AppointmentStatus(str, Enum):
    booked = "booked"
    rescheduled = "rescheduled"
    cancelled = "cancelled"
    completed = "completed"
    no_show = "no_show"


class BookingSource(str, Enum):
    inbound_call = "inbound_call"
    admin_scheduled_call = "admin_scheduled_call"
    # A local Appointment that originated from a Calendly invitee.created webhook with no prior
    # local record — i.e. the doctor sent someone a Calendly invite link directly, or a patient
    # booked through a link in one of Calendly's own emails. See calendly_sync_service.py.
    calendly_direct = "calendly_direct"


class CalendlySchedulingMethod(str, Enum):
    """How THIS specific Appointment row was created — the loop-prevention signal
    calendly_sync_service.py checks before ever creating a new local row from a webhook, so a
    webhook that's just confirming something our own app already pushed doesn't get treated as a
    brand-new external booking. Set once, at row-creation time, never changed afterward.
    """

    api = "api"  # created by appointment_service (our app) — book_first_time/reschedule_existing
    calendly_direct = "calendly_direct"  # created by the inbound webhook/reconciliation handler


class CalendlySyncStatus(str, Enum):
    """Queue state for pushing an app-initiated Appointment to Calendly — consumed by
    workers/tasks/calendly_push_task.py's sweep, not by the admin-facing book/reschedule/cancel
    request itself (which must stay fast and not block on Calendly's latency/retries).
    """

    pending = "pending"  # needs a push (or a cancellation push) to Calendly
    syncing = "syncing"  # claimed by a sweep tick, push in flight
    synced = "synced"  # local state matches Calendly
    failed = "failed"  # last push attempt errored — see Appointment.calendly_sync_error
    not_applicable = "not_applicable"  # nothing to push (e.g. cancelled before ever reaching Calendly)


class CallType(str, Enum):
    inbound = "inbound"
    outbound_admin_scheduled = "outbound_admin_scheduled"


class Direction(str, Enum):
    inbound = "inbound"
    outbound = "outbound"


class CallStatus(str, Enum):
    answered = "answered"
    missed = "missed"
    failed = "failed"
    busy = "busy"
    no_answer = "no_answer"


class CallOutcome(str, Enum):
    appointment_booked = "appointment_booked"
    appointment_rescheduled = "appointment_rescheduled"
    callback_requested = "callback_requested"
    no_action_taken = "no_action_taken"


class CallPurpose(str, Enum):
    admin_scheduled = "admin_scheduled"
    person_requested_callback = "person_requested_callback"


class RequestedBy(str, Enum):
    admin = "admin"
    system = "system"
    person = "person"


class CallScheduleStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    missed = "missed"
    cancelled = "cancelled"


class ActorType(str, Enum):
    admin = "admin"
    system = "system"
    ai_agent = "ai_agent"


class AuditAction(str, Enum):
    create = "create"
    update = "update"
    delete = "delete"
    reschedule = "reschedule"
    cancel = "cancel"
