from datetime import datetime

from pydantic import BaseModel

from app.core.constants import AppointmentStatus, BookingSource


class AppointmentCreate(BaseModel):
    person_id: str
    appointment_datetime: datetime
    duration_minutes: int | None = None
    booking_source: BookingSource
    notes: str | None = None
    created_by_call_id: str | None = None


class AppointmentReschedule(BaseModel):
    new_appointment_datetime: datetime
    notes: str | None = None
    created_by_call_id: str | None = None


class AppointmentCancel(BaseModel):
    reason: str | None = None


class AppointmentOut(BaseModel):
    id: str
    person_id: str
    person_full_name: str | None = None
    person_phone_number: str | None = None
    appointment_datetime: datetime
    duration_minutes: int | None = None
    status: AppointmentStatus
    booking_source: BookingSource
    original_appointment_id: str | None = None
    notes: str | None = None
    created_by_call_id: str | None = None
    created_at: datetime
    updated_at: datetime


class SlotCheckRequest(BaseModel):
    requested_datetime: datetime
    duration_minutes: int | None = None
    exclude_appointment_id: str | None = None


class SlotCheckResponse(BaseModel):
    available: bool
    reason: str | None = None
