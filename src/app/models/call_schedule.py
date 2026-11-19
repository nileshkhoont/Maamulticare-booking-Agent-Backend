from datetime import datetime

import pymongo

from app.core.constants import CallPurpose, CallScheduleStatus, RequestedBy
from app.db.base import TimestampedDocument


class CallSchedule(TimestampedDocument):
    person_id: str  # ref persons._id
    appointment_id: str | None = None  # ref appointments._id
    scheduled_at: datetime
    call_purpose: CallPurpose
    requested_by: RequestedBy
    source_call_id: str | None = None  # ref calls._id — person-requested callback origin
    admin_instructions: str | None = None
    # Free-text note for the ADMIN's own reference (the dashboard's "Notes" field). Never sent to
    # the AI agent — unlike admin_instructions above, which is fed into the call context.
    notes: str | None = None
    status: CallScheduleStatus = CallScheduleStatus.pending
    created_by: str | None = None  # ref admins._id, null if system/agent-generated
    edesy_call_id: str | None = None  # set once outbound_call_task places the call with Edesy
    # When outbound_call_task claimed this row (pending -> in_progress). The stuck-schedule sweep
    # measures "in progress too long" from THIS, not from updated_at — updated_at is only ever set
    # at creation (nothing bumps it on save), so a call scheduled hours ahead looked "stuck" the
    # moment it was dispatched and got swept to missed while the patient was still on the line.
    dispatched_at: datetime | None = None

    class Settings(TimestampedDocument.Settings):
        name = "call_schedules"
        indexes = [
            # Core queue-worker index — "give me all pending calls due now"
            pymongo.IndexModel([("status", pymongo.ASCENDING), ("scheduled_at", pymongo.ASCENDING)]),
            pymongo.IndexModel([("person_id", pymongo.ASCENDING)]),
            pymongo.IndexModel([("source_call_id", pymongo.ASCENDING)], sparse=True),
        ]
