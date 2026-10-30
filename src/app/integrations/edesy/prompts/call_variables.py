"""Per-call template variables sent alongside `context` on outbound `place_call` requests, for
Vani's dashboard-level prompt substitution (Agent editor -> Agent Instructions -> Variables). The
`variables` field name is confirmed correct against Edesy's own API docs
(edesy.in/docs/voice-agent/api/trigger-call).

CONFIRMED LIVE 2026-10-16 (real call, schedule 6ad2089bcf8e52bc755d6926): the AGENT PROMPT TEXT
must reference these with SINGLE curly braces (e.g. "{person_name}"), not double
("{{person_name}}") — Edesy's docs example uses single braces, and a real call sent
variables={"person_name": "Harsh", "previous_appointment_status": "upcoming", ...} correctly, but
the agent greeted with a nameless, Step-1-style generic opener instead of using the name or
routing into the "has an upcoming appointment" branch — i.e. the substitution silently failed
because the prompt's placeholders were double-braced. This Python dict's own key names are
unaffected (they're plain dict keys, read by Edesy regardless of brace style) — only the Agent
Instructions prompt TEXT in Edesy's dashboard needed the brace fix.

The upcoming/expired decision is computed here, in UTC-aware Python, rather than left to the LLM —
language models have no reliable notion of "today's date" mid-conversation, so doing date math in
the prompt would be guesswork. This mirrors render_admin_instructions_context's approach of
pre-rendering call-specific text server-side instead of asking the agent to derive it.

These pre-call variables only ever describe ONE appointment — the one Step 0 opens with — because
they are plain string substitution, computed before the call starts and before any tool has run.
A person can have several upcoming appointments (see appointment_repository.list_upcoming_for_
person); the full list, with real appointment_ids the agent can act on, is deliberately NOT sent
here. It's fetched live via the identify_person tool call instead, so it can never go stale
between when this call was scheduled and when it's actually answered, and so the agent has one
single source of truth for "which appointments exist" and "which id is which" throughout the
call, the same way Step 3C's cancellation flow already works.
"""

from app.models.appointment import Appointment
from app.models.person import Person
from app.utils.datetime_utils import format_ist_human


def build_call_variables(
    person: Person,
    upcoming_appointments: list[Appointment],
    last_expired_appointment: Appointment | None = None,
) -> dict[str, str]:
    """upcoming_appointments: every active appointment still in the future (see
    appointment_repository.list_upcoming_for_person) — pass [] if there are none.
    last_expired_appointment: only consulted when upcoming_appointments is empty; the caller's
    most recent past appointment, if any (e.g. from appointment_repository.get_active_for_person,
    which — precisely because nothing upcoming exists — can only return a past one here).
    """
    if upcoming_appointments:
        previous_status = "upcoming"
        # "Most recently booked" = latest created_at, NOT the furthest-away appointment_datetime
        # — a caller who books Sept 26 and then, in a later call, books Sept 24 as well, should
        # hear about the Sept 24 one in Step 0, since that's the one they actually booked last.
        most_recently_booked = max(upcoming_appointments, key=lambda a: a.created_at)
        previous_datetime_ist = format_ist_human(most_recently_booked.appointment_datetime)
    elif last_expired_appointment is not None:
        previous_status = "expired"
        previous_datetime_ist = format_ist_human(last_expired_appointment.appointment_datetime)
    else:
        previous_status = "none"
        previous_datetime_ist = ""

    return {
        "person_name": person.full_name or "",
        "phone_number": person.phone_number,
        "previous_appointment_status": previous_status,
        "previous_appointment_datetime_ist": previous_datetime_ist,
    }


__all__ = ["build_call_variables"]
