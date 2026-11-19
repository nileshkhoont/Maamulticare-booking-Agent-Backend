from datetime import UTC, datetime

from beanie import PydanticObjectId, UpdateResponse
from beanie.operators import Set

from app.core.constants import CallPurpose, CallScheduleStatus
from app.models.call_schedule import CallSchedule
from app.schemas.common import PageParams


class CallScheduleRepository:
    async def get_by_id(self, schedule_id: str) -> CallSchedule | None:
        if not PydanticObjectId.is_valid(schedule_id):
            return None
        schedule = await CallSchedule.get(schedule_id)
        if schedule is None or schedule.is_deleted:
            return None
        return schedule

    async def get_by_edesy_call_id(self, edesy_call_id: str) -> CallSchedule | None:
        """Correlates an incoming call.ended webhook (call.callSid) back to the call_schedules
        row that dispatched it — edesy_call_id is set on the schedule at dispatch time
        (workers/tasks/outbound_call_task.py), independently of whether a `calls` document for it
        exists yet.
        """
        return await CallSchedule.find_one(
            CallSchedule.edesy_call_id == edesy_call_id, CallSchedule.is_deleted == False  # noqa: E712
        )

    async def list_due(self, as_of: datetime, limit: int = 50) -> list[CallSchedule]:
        """The outbound queue worker's core read: all pending entries due now — backed by the
        {status, scheduled_at} compound index (schema doc §6).
        """
        return (
            await CallSchedule.find(
                CallSchedule.status == CallScheduleStatus.pending,
                CallSchedule.scheduled_at <= as_of,
                CallSchedule.is_deleted == False,  # noqa: E712
            )
            .sort(+CallSchedule.scheduled_at)
            .limit(limit)
            .to_list()
        )

    async def claim_for_dispatch(self, schedule_id: PydanticObjectId) -> CallSchedule | None:
        """Atomically flips ONE schedule pending -> in_progress and stamps dispatched_at, returning
        the claimed row — or None if it's no longer pending (another worker/tick already claimed
        it, or an admin cancelled it in the meantime). A single find_one_and_update, so two
        overlapping dispatch ticks can never both claim — and both place — the same call.
        """
        return await CallSchedule.find_one(
            CallSchedule.id == schedule_id,
            CallSchedule.status == CallScheduleStatus.pending,
            CallSchedule.is_deleted == False,  # noqa: E712
        ).update(
            Set({CallSchedule.status: CallScheduleStatus.in_progress, CallSchedule.dispatched_at: datetime.now(UTC)}),
            response_type=UpdateResponse.NEW_DOCUMENT,
        )

    async def list_stuck_in_progress(self, older_than: datetime) -> list[CallSchedule]:
        """in_progress rows dispatched before `older_than`. Rows dispatched before dispatched_at
        existed (still in_progress at deploy time) have none, so only those fall back to the old
        updated_at comparison — nothing is left stuck forever across the upgrade.
        """
        return await CallSchedule.find(
            CallSchedule.status == CallScheduleStatus.in_progress,
            CallSchedule.is_deleted == False,  # noqa: E712
            {
                "$or": [
                    {"dispatched_at": {"$lte": older_than}},
                    {"dispatched_at": None, "updated_at": {"$lte": older_than}},
                ]
            },
        ).to_list()

    async def mark_missed_if_still_in_progress(self, schedule: CallSchedule) -> bool:
        """Conditional write for the stuck sweep: only flips the row if it is STILL in_progress, so
        a call.ended webhook that landed after the sweep read the row is never overwritten.
        """
        result = await CallSchedule.find_one(
            CallSchedule.id == schedule.id,
            CallSchedule.status == CallScheduleStatus.in_progress,
        ).update(Set({CallSchedule.status: CallScheduleStatus.missed}))
        return result.modified_count == 1

    async def list_filtered(
        self,
        page: PageParams,
        status: CallScheduleStatus | None = None,
        call_purpose: CallPurpose | None = None,
        person_ids: list[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[CallSchedule], int]:
        conditions: list = [CallSchedule.is_deleted == False]  # noqa: E712
        if status:
            conditions.append(CallSchedule.status == status)
        if call_purpose:
            conditions.append(CallSchedule.call_purpose == call_purpose)
        if person_ids is not None:
            conditions.append({"person_id": {"$in": person_ids}})
        if date_from or date_to:
            range_query: dict = {}
            if date_from:
                range_query["$gte"] = date_from
            if date_to:
                range_query["$lte"] = date_to
            conditions.append({"scheduled_at": range_query})

        query = CallSchedule.find(*conditions)
        total = await query.count()
        items = (
            await query.sort(-CallSchedule.scheduled_at)
            .skip(page.skip)
            .limit(page.page_size)
            .to_list()
        )
        return items, total


call_schedule_repository = CallScheduleRepository()
