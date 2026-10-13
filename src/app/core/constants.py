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
