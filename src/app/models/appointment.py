from datetime import datetime

import pymongo
from beanie import Indexed

from app.core.constants import AppointmentStatus, BookingSource
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
        ]
