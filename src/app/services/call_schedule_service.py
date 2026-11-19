from datetime import datetime

from beanie import UpdateResponse
from beanie.operators import Set

from app.core.constants import ActorType, AuditAction, CallPurpose, CallScheduleStatus, RequestedBy
from app.core.exceptions import NotFoundError, ScheduleNotPendingError
from app.models.call_schedule import CallSchedule
from app.repositories.call_schedule_repository import call_schedule_repository
from app.services.audit_service import audit_service
from app.utils.datetime_utils import ensure_utc


def _not_pending_message(status: CallScheduleStatus) -> str:
    if status == CallScheduleStatus.in_progress:
        return "This call has already started and can no longer be cancelled"
    if status == CallScheduleStatus.cancelled:
        return "This call is already cancelled"
    return "This call has already finished"


class CallScheduleService:
    """Queue creation for admin-scheduled calls (PDF §3.1): admin picks a person — either from
    their already-booked appointments or via a fresh schedule-call form — sets a date/time, and
    optionally leaves instructions for the agent to discuss.
    """

    async def create_admin_scheduled(
        self,
        person_id: str,
        scheduled_at: datetime,
        admin_id: str,
        appointment_id: str | None = None,
        admin_instructions: str | None = None,
        notes: str | None = None,
    ) -> CallSchedule:
        schedule = CallSchedule(
            person_id=person_id,
            appointment_id=appointment_id,
            scheduled_at=ensure_utc(scheduled_at),
            call_purpose=CallPurpose.admin_scheduled,
            requested_by=RequestedBy.admin,
            admin_instructions=admin_instructions,
            notes=notes,
            status=CallScheduleStatus.pending,
            created_by=admin_id,
        )
        await schedule.insert()
        await audit_service.record(
            actor_type=ActorType.admin,
            action=AuditAction.create,
            entity_type="call_schedule",
            entity_id=str(schedule.id),
            actor_id=admin_id,
            after=schedule.model_dump(mode="json"),
        )
        return schedule

    async def cancel(self, schedule_id: str, admin_id: str | None = None) -> CallSchedule:
        schedule = await call_schedule_repository.get_by_id(schedule_id)
        if schedule is None:
            raise NotFoundError("Call schedule not found")
        if schedule.status != CallScheduleStatus.pending:
            # Only a call that hasn't been placed yet can be cancelled. The admin's page doesn't
            # refresh on its own, so it can still show "Pending" for a call that is ringing or
            # already finished — accepting that used to rewrite a completed call as "cancelled".
            raise ScheduleNotPendingError(_not_pending_message(schedule.status))

        before = schedule.model_dump(mode="json")
        # One atomic, conditional write that changes ONLY the status, and only while the row is
        # still pending. If the dispatcher claimed it between the read above and this write, the
        # cancel loses (the phone is already ringing) — and nothing else on the row is touched.
        # The previous full-document save() could erase the edesy_call_id dispatch had just
        # stored, so the call.ended webhook couldn't link the call and recorded it as inbound.
        cancelled = await CallSchedule.find_one(
            CallSchedule.id == schedule.id,
            CallSchedule.status == CallScheduleStatus.pending,
        ).update(Set({CallSchedule.status: CallScheduleStatus.cancelled}), response_type=UpdateResponse.NEW_DOCUMENT)
        if cancelled is None:
            current = await call_schedule_repository.get_by_id(schedule_id)
            raise ScheduleNotPendingError(
                _not_pending_message(current.status if current else CallScheduleStatus.in_progress)
            )

        await audit_service.record(
            actor_type=ActorType.admin if admin_id else ActorType.system,
            action=AuditAction.cancel,
            entity_type="call_schedule",
            entity_id=str(cancelled.id),
            actor_id=admin_id,
            before=before,
            after=cancelled.model_dump(mode="json"),
        )
        return cancelled


call_schedule_service = CallScheduleService()
