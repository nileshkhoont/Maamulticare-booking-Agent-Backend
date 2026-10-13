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


class GoogleCalendarSyncStatus(str, Enum):
    """Queue state for pushing an app-initiated Appointment to Google Calendar — consumed by
    workers/tasks/google_calendar_push_task.py's sweep, not by the admin-facing
    book/reschedule/cancel request itself (which must stay fast and not block on Google's
    latency/retries). This is a one-way, outbound-only mirror — there is no inbound direction at
    all, so no "permanently_failed" state is needed: unlike a service with Event-Type-style slot
    reservations, Google Calendar has no concept of a time being "already taken" — calendars
    natively allow overlapping events, so that whole failure class doesn't exist here.
    """

    pending = "pending"  # needs a push (or a deletion push) to Google Calendar
    syncing = "syncing"  # claimed by a sweep tick, push in flight
    synced = "synced"  # local state matches Google Calendar
    failed = "failed"  # last push attempt errored — see Appointment.google_calendar_sync_error
    not_applicable = "not_applicable"  # nothing to push (e.g. cancelled before ever reaching Google Calendar)


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
