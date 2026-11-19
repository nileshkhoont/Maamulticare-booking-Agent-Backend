"""Picks up due call_schedules entries and places the call via Edesy. `place_call` is not
idempotent on Edesy's side, so `call_schedules._id` is passed as the idempotency key
(folder-structure doc's cross-cutting practices table), and a schedule is claimed out of
"pending" with one atomic find_one_and_update before dispatch, so the same due entry is never
picked up twice by overlapping polls.
"""

import asyncio
from datetime import UTC, datetime

from beanie.operators import Set

from app.core.config import settings
from app.core.constants import CallScheduleStatus
from app.core.exceptions import EdesyIntegrationError
from app.core.logging import get_logger
from app.db.mongodb import close_db, connect_db
from app.integrations.edesy.client import edesy_client
from app.integrations.edesy.prompts import build_call_variables, render_admin_instructions_context
from app.models.call_schedule import CallSchedule
from app.models.person import Person
from app.repositories.appointment_repository import appointment_repository
from app.repositories.call_schedule_repository import call_schedule_repository
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def dispatch_due_calls_once() -> int:
    """Core dispatch logic — assumes the DB is already connected. Shared by the standalone Celery
    task below and by workers/inprocess_scheduler.py (a Redis/Celery-free dev fallback that runs
    this same logic on a timer inside the API process itself).
    """
    if not settings.edesy_agent_id:
        logger.warning("outbound_call_task_skipped", reason="EDESY_AGENT_ID not configured")
        return 0
    # EDESY_FROM_NUMBER is intentionally optional, not a dispatch-blocking requirement: when unset,
    # from_number below is None, EdesyPlaceCallRequest.fromNumber is omitted from the request body,
    # and Edesy falls back to its own account-level default number rather than the call failing to
    # go out at all. Set it explicitly whenever possible though — see Settings.edesy_from_number's
    # docstring for why that default was root-caused (2026-09-30) to resolve to the free trial
    # number instead of the hospital's actual purchased number on at least one real account.
    if not settings.edesy_from_number:
        logger.warning(
            "outbound_call_edesy_from_number_not_configured",
            note="Falling back to Edesy's own default number for this dispatch batch.",
        )

    dispatched = 0
    due = await call_schedule_repository.list_due(datetime.now(UTC))
    for candidate in due:
        # Atomic pending -> in_progress claim (also stamps dispatched_at). If it returns None,
        # another overlapping tick already claimed this row, or an admin cancelled it since
        # list_due read it — either way, it must not be placed by this tick.
        schedule = await call_schedule_repository.claim_for_dispatch(candidate.id)
        if schedule is None:
            logger.info("outbound_call_already_claimed", schedule_id=str(candidate.id))
            continue

        # Every write below is a $set of only the field it changes (never schedule.save()): the
        # call.ended webhook (services/call_service.py) can finalize this same row at any moment
        # once the call is placed, and a full-document save would overwrite its result.
        person = await Person.get(schedule.person_id)
        if person is None or person.is_deleted:
            logger.error("outbound_call_person_missing", schedule_id=str(schedule.id))
            await schedule.update(Set({CallSchedule.status: CallScheduleStatus.cancelled}))
            continue

        context = {
            "call_schedule_id": str(schedule.id),
            "person_id": schedule.person_id,
            "appointment_id": schedule.appointment_id,
            "admin_instructions": render_admin_instructions_context(schedule.admin_instructions),
        }
        upcoming_appointments = await appointment_repository.list_upcoming_for_person(schedule.person_id)
        # Only bother looking for a past ("expired") appointment when there's nothing upcoming —
        # it's only ever used for that branch (see build_call_variables).
        last_expired_appointment = (
            None
            if upcoming_appointments
            else await appointment_repository.get_active_for_person(schedule.person_id)
        )
        variables = build_call_variables(person, upcoming_appointments, last_expired_appointment)

        try:
            result = await edesy_client.place_call(
                agent_id=settings.edesy_agent_id,
                phone_number=person.phone_number,
                from_number=settings.edesy_from_number,
                context=context,
                variables=variables,
                idempotency_key=str(schedule.id),
            )
            await schedule.update(Set({CallSchedule.edesy_call_id: result.call_id}))
            dispatched += 1
            logger.info("outbound_call_dispatched", schedule_id=str(schedule.id), edesy_call_id=result.call_id)
        except EdesyIntegrationError as exc:
            # No retry on a dispatch failure either — mark it missed and move on rather than
            # looping. An admin has to schedule a new call if this needs to happen again.
            logger.error(
                "outbound_call_dispatch_failed",
                schedule_id=str(schedule.id),
                error=str(exc),
            )
            await schedule.update(Set({CallSchedule.status: CallScheduleStatus.missed}))
    return dispatched


async def _dispatch_due_calls() -> int:
    """Standalone entry point for the Celery task — owns its own DB connection since it runs in
    a separate worker process from the API.
    """
    await connect_db()
    try:
        return await dispatch_due_calls_once()
    finally:
        await close_db()


@celery_app.task(name="app.workers.tasks.outbound_call_task.dispatch_due_calls")
def dispatch_due_calls() -> int:
    return asyncio.run(_dispatch_due_calls())
