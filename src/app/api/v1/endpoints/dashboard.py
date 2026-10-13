from fastapi import APIRouter, Depends

from app.api.deps import get_current_admin
from app.core.constants import AppointmentStatus, CallScheduleStatus, CallType
from app.models.admin import Admin
from app.models.appointment import Appointment
from app.models.call import Call
from app.models.call_schedule import CallSchedule
from app.schemas.call import DashboardStats

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=DashboardStats)
async def get_dashboard_stats(_: Admin = Depends(get_current_admin)) -> DashboardStats:
    """Aggregated booked/missed/outcome counts for the admin overview page."""
    booked = await Appointment.find(
        Appointment.is_deleted == False, Appointment.status == AppointmentStatus.booked  # noqa: E712
    ).count()
    rescheduled = await Appointment.find(
        Appointment.is_deleted == False, Appointment.status == AppointmentStatus.rescheduled  # noqa: E712
    ).count()
    cancelled = await Appointment.find(
        Appointment.is_deleted == False, Appointment.status == AppointmentStatus.cancelled  # noqa: E712
    ).count()
    inbound = await Call.find(
        Call.is_deleted == False, Call.call_type == CallType.inbound  # noqa: E712
    ).count()
    outbound = await Call.find(
        Call.is_deleted == False, Call.call_type == CallType.outbound_admin_scheduled  # noqa: E712
    ).count()
    admin_scheduled = await CallSchedule.find(
        CallSchedule.is_deleted == False, CallSchedule.call_purpose == "admin_scheduled"  # noqa: E712
    ).count()
    agent_scheduled = await CallSchedule.find(
        CallSchedule.is_deleted == False,  # noqa: E712
        CallSchedule.call_purpose == "person_requested_callback",
    ).count()
    # Scheduled calls that ended up "missed" — the same rows the admin's Schedule page lists under
    # its Missed status (previously this tile counted Call rows with a failed/busy/no_answer status).
    missed = await CallSchedule.find(
        CallSchedule.is_deleted == False, CallSchedule.status == CallScheduleStatus.missed  # noqa: E712
    ).count()

    return DashboardStats(
        booked_appointments=booked,
        rescheduled_appointments=rescheduled,
        cancelled_appointments=cancelled,
        inbound_calls=inbound,
        outbound_calls=outbound,
        admin_scheduled_calls=admin_scheduled,
        agent_scheduled_calls=agent_scheduled,
        missed_calls=missed,
    )
