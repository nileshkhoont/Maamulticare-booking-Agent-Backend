from datetime import datetime

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_admin, get_page_params
from app.core.constants import ActorType, AppointmentStatus, AuditAction
from app.core.exceptions import NotFoundError
from app.models.admin import Admin
from app.models.appointment import Appointment
from app.models.person import Person
from app.repositories.appointment_repository import appointment_repository
from app.repositories.person_repository import person_repository
from app.schemas.appointment import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentOut,
    AppointmentReschedule,
    SlotCheckRequest,
    SlotCheckResponse,
)
from app.schemas.common import Page, PageParams
from app.services.appointment_service import appointment_service
from app.services.audit_service import audit_service
from app.services.slot_service import slot_service
from app.utils.datetime_utils import ensure_utc
from app.utils.pagination import build_page

router = APIRouter(prefix="/appointments", tags=["appointments"])


def _to_out(appointment: Appointment, person: Person | None = None) -> AppointmentOut:
    return AppointmentOut(
        id=str(appointment.id),
        person_id=appointment.person_id,
        person_full_name=person.full_name if person else None,
        person_phone_number=person.phone_number if person else None,
        appointment_datetime=appointment.appointment_datetime,
        duration_minutes=appointment.duration_minutes,
        status=appointment.status,
        booking_source=appointment.booking_source,
        original_appointment_id=appointment.original_appointment_id,
        notes=appointment.notes,
        created_by_call_id=appointment.created_by_call_id,
        created_at=appointment.created_at,
        updated_at=appointment.updated_at,
    )


@router.get("", response_model=Page[AppointmentOut])
async def list_appointments(
    status: AppointmentStatus | None = Query(default=None),
    q: str | None = Query(default=None, description="Search by person name or phone number"),
    date_from: datetime | None = Query(default=None, description="appointment_datetime >= (inclusive)"),
    date_to: datetime | None = Query(default=None, description="appointment_datetime <= (inclusive)"),
    page: PageParams = Depends(get_page_params),
    _: Admin = Depends(get_current_admin),
) -> Page[AppointmentOut]:
    person_ids: list[str] | None = None
    if q:
        person_ids = await person_repository.find_ids_matching(q)
        if not person_ids:
            return build_page([], 0, page)

    items, total = await appointment_repository.list_filtered(
        page,
        status=status,
        person_ids=person_ids,
        date_from=ensure_utc(date_from) if date_from else None,
        date_to=ensure_utc(date_to) if date_to else None,
    )
    persons = await person_repository.get_many_by_ids({a.person_id for a in items})
    return build_page([_to_out(a, persons.get(a.person_id)) for a in items], total, page)


@router.get("/for-person/{person_id}", response_model=list[AppointmentOut])
async def list_for_person(person_id: str, _: Admin = Depends(get_current_admin)) -> list[AppointmentOut]:
    items = await appointment_repository.list_for_person(person_id)
    return [_to_out(a) for a in items]


@router.get("/bookable", response_model=list[AppointmentOut])
async def list_bookable_for_schedule_form(
    person_id: str | None = Query(default=None),
    _: Admin = Depends(get_current_admin),
) -> list[AppointmentOut]:
    """Feeds the schedule/new page's "pick from already booked appointments" list (PDF §3.1)."""
    items = await appointment_repository.list_booked_for_scheduling(person_id)
    return [_to_out(a) for a in items]


@router.post("/check-slot", response_model=SlotCheckResponse)
async def check_slot(payload: SlotCheckRequest, _: Admin = Depends(get_current_admin)) -> SlotCheckResponse:
    return await slot_service.check_availability(
        payload.requested_datetime, exclude_appointment_id=payload.exclude_appointment_id
    )


@router.get("/{appointment_id}", response_model=AppointmentOut)
async def get_appointment(appointment_id: str, _: Admin = Depends(get_current_admin)) -> AppointmentOut:
    appointment = await appointment_repository.get_by_id(appointment_id)
    if appointment is None:
        raise NotFoundError("Appointment not found")
    return _to_out(appointment)


@router.post("", response_model=AppointmentOut, status_code=201)
async def create_appointment(
    payload: AppointmentCreate, current: Admin = Depends(get_current_admin)
) -> AppointmentOut:
    appointment = await appointment_service.book_first_time(
        person_id=payload.person_id,
        appointment_datetime=payload.appointment_datetime,
        booking_source=payload.booking_source,
        duration_minutes=payload.duration_minutes,
        notes=payload.notes,
        created_by_call_id=payload.created_by_call_id,
    )
    await audit_service.record(
        actor_type=ActorType.admin,
        action=AuditAction.create,
        entity_type="appointment",
        entity_id=str(appointment.id),
        actor_id=str(current.id),
        after=_to_out(appointment).model_dump(mode="json"),
    )
    return _to_out(appointment)


@router.post("/{appointment_id}/reschedule", response_model=AppointmentOut)
async def reschedule_appointment(
    appointment_id: str, payload: AppointmentReschedule, current: Admin = Depends(get_current_admin)
) -> AppointmentOut:
    new_appointment = await appointment_service.reschedule_existing(
        appointment_id=appointment_id,
        new_appointment_datetime=payload.new_appointment_datetime,
        notes=payload.notes,
        created_by_call_id=payload.created_by_call_id,
    )
    await audit_service.record(
        actor_type=ActorType.admin,
        action=AuditAction.reschedule,
        entity_type="appointment",
        entity_id=str(new_appointment.id),
        actor_id=str(current.id),
        before={"original_appointment_id": appointment_id},
        after=_to_out(new_appointment).model_dump(mode="json"),
    )
    return _to_out(new_appointment)


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut)
async def cancel_appointment(
    appointment_id: str, payload: AppointmentCancel, current: Admin = Depends(get_current_admin)
) -> AppointmentOut:
    appointment = await appointment_service.cancel(appointment_id, reason=payload.reason)
    await audit_service.record(
        actor_type=ActorType.admin,
        action=AuditAction.cancel,
        entity_type="appointment",
        entity_id=str(appointment.id),
        actor_id=str(current.id),
        after=_to_out(appointment).model_dump(mode="json"),
    )
    return _to_out(appointment)
