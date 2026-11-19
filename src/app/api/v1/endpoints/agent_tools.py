"""The functions Edesy's agent invokes *while on the call* (folder-structure doc). Secured by
`core.tool_auth.require_tool_secret` — a shared secret, not an admin JWT, since Edesy's servers
call these directly with no admin session in play.

`identify_person` is one addition beyond the folder-structure doc's original four tools
(`check_slot_availability`, `book_appointment`, `reschedule_appointment`,
`log_callback_request`): the documented inbound-call flow requires the agent to identify the
caller and learn whether they already have an appointment (PDF §2.1) before it can decide which
branch to take, and no other tool covers that — so it's a necessary part of the documented
architecture, not an invented feature.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends
from pydantic import BaseModel, BeforeValidator

from app.core.constants import AppointmentStatus, BookingSource
from app.core.exceptions import AppError, AppointmentNotActiveError
from app.core.logging import get_logger
from app.core.tool_auth import require_tool_secret
from app.models.appointment import Appointment
from app.models.person import Person
from app.repositories.call_schedule_repository import call_schedule_repository
from app.repositories.person_repository import person_repository
from app.schemas.call import ToolResponse
from app.services.appointment_service import appointment_service
from app.services.callback_service import callback_service
from app.services.slot_service import slot_service
from app.utils.datetime_utils import ensure_utc, format_ist_human, format_ist_time
from app.utils.validators import is_unresolved_placeholder, strip_unresolved_placeholder

logger = get_logger(__name__)

router = APIRouter(
    prefix="/agent-tools", tags=["agent_tools"], dependencies=[Depends(require_tool_secret)]
)


def _reject_unresolved(value: str) -> str:
    """For *required* fields, where silently dropping the value isn't an option — a literal
    `{{call.phone_number}}` must fail loudly (422) rather than get used as a real phone number and
    create a junk Person, which is exactly what makes these bugs invisible until someone notices
    corrupted data days later.
    """
    if is_unresolved_placeholder(value):
        raise ValueError(
            "received an unresolved template token instead of a value — check this tool's "
            "Request Body mapping in the Edesy dashboard"
        )
    return value


# Every optional free-text/id field an Edesy Custom Function can send. Edesy posts unfilled
# optional parameters as the literal token (see utils/validators.py), so each one is normalised
# back to None at the boundary — before any of it can reach a database write.
OptionalToolStr = Annotated[str | None, BeforeValidator(strip_unresolved_placeholder)]
RequiredToolStr = Annotated[str, BeforeValidator(_reject_unresolved)]


def _strip_placeholder_before_datetime(value: object) -> object:
    """Same reasoning as strip_unresolved_placeholder, for an optional datetime field — Edesy
    posts the literal unrendered token when the model leaves it blank, which is not a parseable
    datetime and would 422 every call that legitimately has nothing to send here, so it must be
    normalised to None before Pydantic's datetime parser ever sees it.
    """
    if isinstance(value, str) and is_unresolved_placeholder(value):
        return None
    return value


OptionalToolDateTime = Annotated[datetime | None, BeforeValidator(_strip_placeholder_before_datetime)]


async def _resolve_person_for_call(
    phone_number: str,
    edesy_call_id: str | None,
    full_name: str | None = None,
    *,
    overwrite_existing_name: bool = True,
) -> Person:
    """The authoritative identity for every agent-tool call that reads or changes appointment
    data — this is what actually enforces "an appointment can only be reached from the phone
    number the current call is really with", not just prompt wording.

    For an OUTBOUND call, edesy_call_id (Edesy's own {{call.sid}} call-context token — never an
    LLM-fillable parameter, same as phone_number is meant to be) is looked up against
    call_schedules, which was written by OUR OWN dispatch code
    (workers/tasks/outbound_call_task.py) the moment we placed the call — before Edesy, the LLM,
    or the person on the other end of the line were ever involved. When a match exists, THAT
    schedule's person is used and payload.phone_number is not consulted at all. This is
    deliberate: if a caller says "cancel my friend's appointment, his number is ...", or if a
    dashboard's Request Body ever mistakenly exposes phone_number as something the model can
    fill in, it still cannot change whose appointment gets looked up, rescheduled, or cancelled
    on an outbound call — the schedule fixes that before the call starts.

    Falls back to phone_number-based lookup otherwise. That fallback is the ONLY path available
    for INBOUND calls: Edesy's platform only ever fires one webhook, call.ended, AFTER the call
    is already over (see integrations/edesy/webhook_events.py) — there is no call.started
    signal, so nothing exists server-side to independently verify an inbound caller's number
    against before the call happens. phone_number there is Edesy's own {{call.phone_number}}
    call-context token, resolved by Edesy's telephony layer from the real caller ID — trusted the
    same way it always has been, which is why it is critical that every tool's Request Body in
    the Edesy dashboard maps phone_number to that token and does NOT list it as an LLM-fillable
    parameter (see the prompt-update guidance delivered alongside this change).

    overwrite_existing_name: False for identify_person specifically (see its own call site) —
    2026-09-23 incident: a caller asked to check a FRIEND's appointment and stated the friend's
    name; the model passed that name straight through as full_name. Because identity here always
    resolves to the CALLER's own real phone number/schedule (exactly as designed above), that
    silently renamed the caller's own record to the friend's name — and identify_person's
    response then echoed that new name straight back, making the model believe it actually WAS
    looking at the friend's data, compounding the confusion for the rest of the call. A brand
    new Person has no existing name to protect, so full_name is still used to seed one on first
    contact either way — only an UPDATE to an already-known name is ever skipped.
    """
    if edesy_call_id:
        schedule = await call_schedule_repository.get_by_edesy_call_id(edesy_call_id)
        if schedule is not None:
            person = await person_repository.get_by_id(schedule.person_id)
            if person is not None:
                if person.phone_number != phone_number:
                    logger.warning(
                        "agent_tool_phone_number_mismatch_ignored",
                        edesy_call_id=edesy_call_id,
                        scheduled_phone_number=person.phone_number,
                        payload_phone_number=phone_number,
                    )
                if overwrite_existing_name:
                    return await person_repository.apply_name_if_given(person, full_name)
                return person

    if overwrite_existing_name:
        return await person_repository.get_or_create_by_phone(phone_number, full_name)

    existing = await person_repository.get_by_phone(phone_number)
    if existing is not None:
        return existing
    return await person_repository.get_or_create_by_phone(phone_number, full_name)


def _serialize_upcoming(appointments: list[Appointment]) -> list[dict]:
    """Every upcoming appointment, soonest first — how identify_person exposes the full list,
    and what a "multiple upcoming appointments" disambiguation response carries back too, so the
    agent always has real appointment_ids to act on rather than a date it half-remembers from
    earlier in the call.
    """
    return [
        {
            "appointment_id": str(a.id),
            "appointment_datetime": a.appointment_datetime.isoformat(),
            "appointment_datetime_ist": format_ist_human(a.appointment_datetime),
        }
        for a in sorted(appointments, key=lambda a: a.appointment_datetime)
    ]


async def _resolve_appointment_id_or_disambiguate(
    person: Person,
    requested_appointment_id: str | None,
    action: str,
    expected_appointment_datetime: datetime | None = None,
) -> tuple[str | None, ToolResponse | None]:
    """Shared by reschedule_appointment and cancel_appointment. Returns (appointment_id, None)
    when it's safe to proceed, or (None, an early ToolResponse to return as-is) when it isn't.

    A caller can have more than one upcoming appointment — if the agent didn't pass a valid
    appointment_id (e.g. it never called identify_person, or the caller described one from
    memory instead of picking from a list), guessing which of several upcoming appointments they
    meant would risk rescheduling/cancelling the wrong one. That's only ambiguous when there are
    2+; with zero there's nothing to act on, and with exactly one there's nothing to disambiguate.

    expected_appointment_datetime is the hard technical gate on top of that, added after a
    prompt-only "please double-check the id" instruction (2026-09-23) still wasn't enough: three
    times now in real calls the agent correctly SPOKE the right time to the caller but sent a
    DIFFERENT appointment's id to the tool, silently acting on the wrong appointment while the
    one the caller actually meant sat untouched. The first version of this check only ran when
    the agent chose to send expected_appointment_datetime — which it can simply forget to do
    (still optional in the Edesy dashboard's Parameters, or just not top of mind), and on the
    very next real call after this guard shipped, it did exactly that, and the guard never fired.
    So the check is no longer opt-in: whenever this person actually HAS more than one upcoming
    appointment, expected_appointment_datetime is mandatory, full stop, regardless of whether the
    agent thinks it's needed — deciding "was this ambiguous?" is taken out of the agent's hands
    entirely and driven by the real data instead. Only when there is a single unambiguous
    appointment (nothing to mismatch against) can it be omitted.
    """
    upcoming = await appointment_service.list_upcoming_for_person(str(person.id))

    if requested_appointment_id and PydanticObjectId.is_valid(requested_appointment_id):
        if expected_appointment_datetime is None:
            # Same ownership rule the expected_appointment_datetime branch below enforces: an id
            # belonging to a DIFFERENT person is never acted on. This shortcut used to pass the
            # LLM's id straight through whenever the caller had 0-1 upcoming appointments, so an
            # id from anywhere else could cancel/reschedule someone else's appointment.
            requested = await appointment_service.get_by_id(requested_appointment_id)
            if requested is not None and requested.person_id != str(person.id):
                logger.warning(
                    "agent_tool_appointment_owner_mismatch",
                    action=action,
                    appointment_id=requested_appointment_id,
                    caller_person_id=str(person.id),
                )
                return None, ToolResponse(
                    success=False, message=f"No active appointment found to {action}"
                )
            if len(upcoming) > 1:
                return None, ToolResponse(
                    success=False,
                    message=(
                        "This person has more than one upcoming appointment, so "
                        "expected_appointment_datetime is required together with appointment_id "
                        "— it was not provided. Find the entry in upcoming_appointments below "
                        "matching what you just confirmed with the caller and send both its "
                        "appointment_id and its appointment_datetime."
                    ),
                    data={"upcoming_appointments": _serialize_upcoming(upcoming)},
                )
            return requested_appointment_id, None

        appointment = await appointment_service.get_by_id(requested_appointment_id)
        if appointment is None or appointment.person_id != str(person.id):
            # Never touch a row that isn't this verified caller's own — should be unreachable
            # given phone-number binding, but no silent fallthrough either way.
            return None, ToolResponse(
                success=False, message=f"No active appointment found to {action}"
            )
        expected_utc = ensure_utc(expected_appointment_datetime)
        if appointment.appointment_datetime != expected_utc:
            logger.warning(
                "agent_tool_appointment_id_datetime_mismatch",
                action=action,
                appointment_id=requested_appointment_id,
                actual_datetime=appointment.appointment_datetime.isoformat(),
                expected_datetime=expected_utc.isoformat(),
            )
            return None, ToolResponse(
                success=False,
                message=(
                    "appointment_id does not match expected_appointment_datetime — this is "
                    "not the appointment you just confirmed with the caller. Find the entry "
                    "in upcoming_appointments below whose appointment_datetime_ist equals "
                    f"{format_ist_human(expected_utc)} and use THAT entry's appointment_id."
                ),
                data={"upcoming_appointments": _serialize_upcoming(upcoming)},
            )
        return requested_appointment_id, None

    if not upcoming:
        return None, ToolResponse(success=False, message=f"No active appointment found to {action}")
    if len(upcoming) == 1:
        return str(upcoming[0].id), None

    return None, ToolResponse(
        success=False,
        message=(
            f"This person has {len(upcoming)} upcoming appointments — ask the caller which one "
            f"they mean (read back each appointment_datetime_ist below), then call this again "
            f"with that appointment's appointment_id AND its appointment_datetime as "
            f"expected_appointment_datetime."
        ),
        data={"upcoming_appointments": _serialize_upcoming(upcoming)},
    )


class IdentifyPersonRequest(BaseModel):
    phone_number: RequiredToolStr
    # Only ever used to seed a brand-new Person's name on first contact — never to overwrite an
    # existing one. See _resolve_person_for_call's overwrite_existing_name docstring: this tool
    # is a lookup, called well before any "is this really your own name?" confirmation exists
    # elsewhere in a flow, so it must never have the power to rename someone who's already known.
    full_name: OptionalToolStr = None
    # Same reasoning as BookAppointmentRequest.edesy_call_id — passing this lets identity be
    # resolved from the verified outbound call_schedule instead of the phone_number field alone.
    edesy_call_id: OptionalToolStr = None


@router.post("/identify-person", response_model=ToolResponse)
async def identify_person(payload: IdentifyPersonRequest) -> ToolResponse:
    person = await _resolve_person_for_call(
        payload.phone_number,
        payload.edesy_call_id,
        payload.full_name,
        overwrite_existing_name=False,
    )
    # Only ever future appointments — a booked-but-past appointment must never be reported as
    # something the caller "still has" (2026-09-23 finding).
    upcoming = await appointment_service.list_upcoming_for_person(str(person.id))
    # The most recently BOOKED one (not necessarily the soonest date) — kept as flat top-level
    # fields for backward-compatible single-appointment mentions (Step 0's opening line, Step
    # 3C Turn 2). For anything involving more than one appointment, use upcoming_appointments.
    most_recently_booked = max(upcoming, key=lambda a: a.created_at) if upcoming else None

    return ToolResponse(
        success=True,
        data={
            "person_id": str(person.id),
            "full_name": person.full_name,
            "has_active_appointment": bool(upcoming),
            "appointment_id": str(most_recently_booked.id) if most_recently_booked else None,
            "appointment_datetime": (
                most_recently_booked.appointment_datetime.isoformat() if most_recently_booked else None
            ),
            # Speak this — pre-formatted in India Standard Time, the business's operating zone.
            "appointment_datetime_ist": (
                format_ist_human(most_recently_booked.appointment_datetime)
                if most_recently_booked
                else None
            ),
            # Every upcoming appointment, soonest first, with its real appointment_id — use this
            # to match whichever one the caller describes, and to reschedule/cancel the correct
            # one when there's more than one. Never guess an id or reuse one from memory.
            "upcoming_appointments": _serialize_upcoming(upcoming),
        },
    )


class CheckSlotRequest(BaseModel):
    requested_datetime: datetime
    exclude_appointment_id: OptionalToolStr = None


@router.post("/check-slot-availability", response_model=ToolResponse)
async def check_slot_availability(payload: CheckSlotRequest) -> ToolResponse:
    result = await slot_service.check_availability(
        payload.requested_datetime, exclude_appointment_id=payload.exclude_appointment_id
    )
    return ToolResponse(
        success=True,
        data={
            "available": result.available,
            "reason": result.reason,
            "requested_datetime_ist": format_ist_human(payload.requested_datetime),
            # So a "Business is closed on Fridays" rejection can be answered with the days that
            # ARE open, instead of the agent guessing them from its prompt.
            "working_days": await slot_service.get_working_days(),
        },
    )


class ListAvailableSlotsRequest(BaseModel):
    date: date  # "2026-09-22" — the calendar date the caller is interested in


@router.post("/list-available-slots", response_model=ToolResponse)
async def list_available_slots(payload: ListAvailableSlotsRequest) -> ToolResponse:
    """Every real, currently-open slot on a given date — generated from business_config and
    filtered against actual bookings, live. Use this to offer the caller real alternative times
    (e.g. when their first preference is unavailable) instead of guessing — never state a time is
    open without it appearing in this list or having just passed check_slot_availability.

    When count is 0, `is_working_day` says which of the two very different situations it is: a
    day the business is shut (`false` — tell the caller that, and which days `working_days`
    says are open) versus an open day that's fully booked (`true`).
    """
    day = await slot_service.describe_day(payload.date)
    return ToolResponse(
        success=True,
        data={
            "date": payload.date.isoformat(),
            "count": len(day.slots),
            "slots": [
                {"datetime": s.isoformat(), "time_ist": format_ist_time(s)} for s in day.slots
            ],
            "is_working_day": day.is_working_day,
            "closed_reason": day.closed_reason,
            "working_days": day.working_days,
        },
    )


class BookAppointmentRequest(BaseModel):
    # Resolved server-side from Edesy's own call-context variable ({{call.phone_number}}), not
    # taken as an LLM-supplied parameter — a model can and does mis-remember/hallucinate an
    # opaque person_id across turns (observed 2026-09-21: a call booked with person_id="none"
    # because the agent's prompt never called identify_person). The phone number is the one
    # value Edesy resolves live from the call itself, so resolving the Person from it here
    # guarantees every appointment has a real person_id regardless of what the prompt does.
    phone_number: RequiredToolStr
    requested_datetime: datetime
    # No call_id field here on purpose — our internal calls._id doesn't exist yet at this point
    # (that document is only created when the call.ended webhook arrives, after the call is
    # already over), so there was never a real value an agent could supply for it. It was
    # observed sending "none", made-up placeholders, or Edesy's own call id (the wrong id
    # entirely).
    #
    # edesy_call_id is different: it's Edesy's own call-context variable ({{call.sid}}),
    # resolved live by Edesy's platform, not guessed by the LLM — so it's exact and globally
    # unique per call even when the same person has two calls at once. call_service.py uses it
    # for an exact-match correlation once the call.ended webhook creates the real Call document,
    # falling back to a person+timing heuristic only when this wasn't provided (e.g. an older
    # dashboard config, or the prompt genuinely doesn't have it).
    edesy_call_id: OptionalToolStr = None
    # If the caller states/confirms their name during this call (e.g. a call that was scheduled
    # with only a phone number), pass it here so it actually reaches the Person record — the
    # same update-if-different logic identify_person already uses. Optional: omitted or None
    # leaves the existing name untouched.
    full_name: OptionalToolStr = None


@router.post("/book-appointment", response_model=ToolResponse)
async def book_appointment(payload: BookAppointmentRequest) -> ToolResponse:
    person = await _resolve_person_for_call(
        payload.phone_number, payload.edesy_call_id, payload.full_name
    )
    # This tool is used on BOTH directions of call. A call we placed ourselves has a call_schedules
    # row keyed by this edesy_call_id (written at dispatch time, before the call existed), so its
    # presence is what marks the call as outbound — previously every appointment was hardcoded
    # "inbound_call", even ones the agent booked during an admin-scheduled call.
    booking_source = BookingSource.inbound_call
    if payload.edesy_call_id and await call_schedule_repository.get_by_edesy_call_id(payload.edesy_call_id):
        booking_source = BookingSource.admin_scheduled_call

    try:
        appointment = await appointment_service.book_first_time(
            person_id=str(person.id),
            appointment_datetime=payload.requested_datetime,
            booking_source=booking_source,
            pending_edesy_call_id=payload.edesy_call_id,
        )
    except AppError as exc:
        return ToolResponse(success=False, message=exc.message)

    return ToolResponse(
        success=True,
        data={
            "appointment_id": str(appointment.id),
            "appointment_datetime": appointment.appointment_datetime.isoformat(),
            "appointment_datetime_ist": format_ist_human(appointment.appointment_datetime),
            "status": appointment.status.value,
        },
    )


class RescheduleAppointmentRequest(BaseModel):
    # Same reasoning as BookAppointmentRequest.phone_number — resolved from Edesy's call context,
    # not trusted from the LLM. appointment_id is still accepted (from identify_person's result,
    # if the prompt calls it) but is optional: if missing or stale, we fall back to the person's
    # current active appointment looked up server-side, so a reschedule still succeeds correctly
    # even when the model never captured a valid appointment_id.
    phone_number: RequiredToolStr
    new_appointment_datetime: datetime
    appointment_id: OptionalToolStr = None
    # The CURRENT (before-change) appointment's own appointment_datetime — send this whenever
    # appointment_id came from picking one out of several in upcoming_appointments (never needed
    # when there's only one appointment to begin with). The backend verifies appointment_id
    # actually has this exact appointment_datetime before doing anything, and refuses instead of
    # silently rescheduling the wrong one if they don't match. Added 2026-09-23 after a real call
    # confirmed "1:00 PM" out loud to the caller but sent a different appointment's id — this is
    # what catches that the moment it happens, instead of relying on getting it right unchecked.
    expected_appointment_datetime: OptionalToolDateTime = None
    edesy_call_id: OptionalToolStr = None  # same reasoning as BookAppointmentRequest.edesy_call_id
    full_name: OptionalToolStr = None  # same reasoning as BookAppointmentRequest.full_name


@router.post("/reschedule-appointment", response_model=ToolResponse)
async def reschedule_appointment(payload: RescheduleAppointmentRequest) -> ToolResponse:
    person = await _resolve_person_for_call(
        payload.phone_number, payload.edesy_call_id, payload.full_name
    )

    appointment_id, early_response = await _resolve_appointment_id_or_disambiguate(
        person,
        payload.appointment_id,
        action="reschedule",
        expected_appointment_datetime=payload.expected_appointment_datetime,
    )
    if early_response is not None:
        return early_response

    try:
        appointment = await appointment_service.reschedule_existing(
            appointment_id=appointment_id,
            new_appointment_datetime=payload.new_appointment_datetime,
            pending_edesy_call_id=payload.edesy_call_id,
        )
    except AppError as exc:
        return ToolResponse(success=False, message=exc.message)

    return ToolResponse(
        success=True,
        data={
            "appointment_id": str(appointment.id),
            "appointment_datetime": appointment.appointment_datetime.isoformat(),
            "appointment_datetime_ist": format_ist_human(appointment.appointment_datetime),
            "status": appointment.status.value,
        },
    )


class CancelAppointmentRequest(BaseModel):
    # Same reasoning as RescheduleAppointmentRequest — phone_number resolved from call context;
    # appointment_id is optional and falls back to the person's real active appointment
    # server-side, so the agent can never cancel a wrong or nonexistent appointment based on a
    # misremembered date the caller stated (observed 2026-09-22: a caller said "24 September"
    # when their real appointment was on the 26th — trusting that date directly would have either
    # cancelled nothing or, worse, silently done nothing while claiming success).
    phone_number: RequiredToolStr
    appointment_id: OptionalToolStr = None
    # Same reasoning as RescheduleAppointmentRequest.expected_appointment_datetime — send this
    # whenever appointment_id was picked out of a multi-appointment list.
    expected_appointment_datetime: OptionalToolDateTime = None
    reason: OptionalToolStr = None
    edesy_call_id: OptionalToolStr = None  # same reasoning as BookAppointmentRequest.edesy_call_id


@router.post("/cancel-appointment", response_model=ToolResponse)
async def cancel_appointment(payload: CancelAppointmentRequest) -> ToolResponse:
    person = await _resolve_person_for_call(payload.phone_number, payload.edesy_call_id)

    appointment_id, early_response = await _resolve_appointment_id_or_disambiguate(
        person,
        payload.appointment_id,
        action="cancel",
        expected_appointment_datetime=payload.expected_appointment_datetime,
    )
    if early_response is not None:
        return early_response

    try:
        appointment = await appointment_service.cancel(appointment_id, reason=payload.reason)
    except AppointmentNotActiveError as exc:
        # Asked to cancel an appointment that is ALREADY cancelled (e.g. the agent repeats the
        # call): nothing is changed or re-sent, but the agent gets the same success reply it
        # always has — the caller's appointment is cancelled either way.
        existing = await appointment_service.get_by_id(appointment_id)
        if existing is None or existing.status != AppointmentStatus.cancelled:
            return ToolResponse(success=False, message=exc.message)
        return ToolResponse(
            success=True,
            message="This appointment was already cancelled — nothing was changed.",
            data={
                "appointment_id": str(existing.id),
                "appointment_datetime": existing.appointment_datetime.isoformat(),
                "appointment_datetime_ist": format_ist_human(existing.appointment_datetime),
                "status": existing.status.value,
            },
        )
    except AppError as exc:
        return ToolResponse(success=False, message=exc.message)

    return ToolResponse(
        success=True,
        data={
            "appointment_id": str(appointment.id),
            "appointment_datetime": appointment.appointment_datetime.isoformat(),
            "appointment_datetime_ist": format_ist_human(appointment.appointment_datetime),
            "status": appointment.status.value,
        },
    )


def _blank_before_int(value: object) -> object:
    """Optional integer tool field: Edesy/the LLM may send "", "none", "null" or the unrendered
    token when it has nothing to say — normalise those to None before Pydantic's int parser.
    """
    if isinstance(value, str):
        if is_unresolved_placeholder(value) or value.strip().lower() in {"", "none", "null"}:
            return None
    return value


class LogCallbackRequest(BaseModel):
    # Same reasoning as BookAppointmentRequest.phone_number — resolved from Edesy's call context,
    # not trusted from the LLM.
    phone_number: RequiredToolStr
    # Either an absolute time (IST unless it carries an offset) OR minutes_from_now — for a
    # relative request ("call me in 5 minutes") the agent must NOT do clock math itself: it has no
    # reliable current time, and on 2026-09-23 it sent its own UTC "now + 5 min" as a naive time,
    # which the backend (rightly) reads as IST -> "Requested time is in the past", so the callback
    # was never scheduled. minutes_from_now is computed here from the server's real clock.
    requested_datetime: Annotated[datetime | None, BeforeValidator(_strip_placeholder_before_datetime)] = None
    minutes_from_now: Annotated[int | None, BeforeValidator(_blank_before_int)] = None
    # Optional — the agent has no way to know our internal calls._id mid-conversation (that
    # document doesn't exist until the call.ended webhook arrives, after the call is already
    # over; see integrations/edesy/webhook_events.py). Was required before 2026-09-17, which made
    # this tool impossible for the agent to ever call successfully — kept here only in case Vani
    # exposes its own call reference to the agent, purely informational if provided.
    source_call_id: OptionalToolStr = None
    appointment_id: OptionalToolStr = None
    full_name: OptionalToolStr = None  # same reasoning as BookAppointmentRequest.full_name
    edesy_call_id: OptionalToolStr = None  # same reasoning as BookAppointmentRequest.edesy_call_id


@router.post("/log-callback-request", response_model=ToolResponse)
async def log_callback_request(payload: LogCallbackRequest) -> ToolResponse:
    if payload.minutes_from_now is not None:
        if payload.minutes_from_now < 1:
            return ToolResponse(success=False, message="minutes_from_now must be at least 1")
        requested = datetime.now(UTC) + timedelta(minutes=payload.minutes_from_now)
    elif payload.requested_datetime is not None:
        requested = payload.requested_datetime
    else:
        return ToolResponse(
            success=False,
            message="Send either minutes_from_now (e.g. 5) or requested_datetime in IST",
        )

    person = await _resolve_person_for_call(
        payload.phone_number, payload.edesy_call_id, payload.full_name
    )
    try:
        schedule = await callback_service.create_callback(
            person_id=str(person.id),
            requested_datetime=requested,
            source_call_id=payload.source_call_id,
            appointment_id=payload.appointment_id,
        )
    except AppError as exc:
        # Include the real current time so the agent can correct itself and retry in the same
        # call instead of giving up (it has no trustworthy clock of its own).
        return ToolResponse(
            success=False,
            message=f"{exc.message}. The current time is {format_ist_human(datetime.now(UTC))} — "
            "times are IST; for a relative request send minutes_from_now instead.",
        )

    return ToolResponse(
        success=True,
        data={
            "call_schedule_id": str(schedule.id),
            "scheduled_at": schedule.scheduled_at.isoformat(),
            "scheduled_at_ist": format_ist_human(schedule.scheduled_at),
        },
    )
