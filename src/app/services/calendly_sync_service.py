"""Two-way Calendly sync: pushing our own book/reschedule/cancel actions out to Calendly, and
folding Calendly-side changes (a doctor-sent invite link, or a patient using the links in
Calendly's own emails) back into our system.

Our MongoDB stays authoritative throughout — Calendly is a synced mirror, never the other way
around. Outbound pushes are poll-based (workers/tasks/calendly_push_task.py), not fired inline
from appointment_service.py, matching this codebase's existing convention of never calling an
external integration synchronously from an admin-facing request (see outbound_call_task.py for
the same pattern on the unrelated Edesy outbound-call queue).

Correlation/idempotency follows the exact same external-id idiom as call_service.py's
handle_call_ended / _link_appointment_created_during_call: a dedicated nullable
`calendly_invitee_uri` field, set once an outbound push actually succeeds, looked up via a plain
equality match in the repository — see Appointment's Calendly fields for the full reasoning,
including the race-guard needed because Calendly can webhook back before our own push's response
has been written to the local row.
"""

from datetime import datetime

from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.constants import (
    AppointmentStatus,
    BookingSource,
    CalendlySchedulingMethod,
    CalendlySyncStatus,
)
from app.core.exceptions import CalendlyIntegrationError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.integrations.calendly.client import calendly_client
from app.integrations.calendly.schemas import (
    CalendlyCreateInviteeRequest,
    CalendlyInviteeInput,
    CalendlyLocationInput,
    CalendlyQuestionAndAnswer,
)
from app.integrations.calendly.webhook_events import CalendlyWebhookEvent, CalendlyWebhookInviteePayload
from app.models.appointment import Appointment
from app.models.business_config import BusinessConfig
from app.models.person import Person
from app.repositories.appointment_repository import appointment_repository
from app.repositories.person_repository import person_repository
from app.utils.datetime_utils import ensure_utc

logger = get_logger(__name__)

_DEFAULT_TIMEZONE = "Asia/Kolkata"  # fallback only — see _resolve_timezone


def _event_uuid_from_uri(uri: str) -> str:
    """Calendly URIs are "https://api.calendly.com/scheduled_events/<uuid>" — the API's own
    cancellation/GET-by-id endpoints want just the trailing uuid, not the full URI.
    """
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _parse_calendly_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        logger.warning("calendly_datetime_unparseable", value=value)
        return None


class CalendlySyncService:
    async def _resolve_timezone(self) -> str:
        config = await BusinessConfig.find_one({})
        return config.timezone if config and config.timezone else _DEFAULT_TIMEZONE

    def _build_invitee_input(self, person: Person, timezone: str) -> tuple[CalendlyInviteeInput, bool]:
        """Returns (invitee_input, is_placeholder_email). Real email if Person.email is set and
        non-blank; otherwise the fixed placeholder — see settings.calendly_placeholder_email's
        docstring. Phone is always passed via text_reminder_number regardless of which email was
        used, so Calendly's SMS reminders work either way.
        """
        real_email = (person.email or "").strip()
        if real_email:
            return (
                CalendlyInviteeInput(
                    name=person.full_name or person.phone_number,
                    email=real_email,
                    timezone=timezone,
                    text_reminder_number=person.phone_number,
                ),
                False,
            )
        return (
            CalendlyInviteeInput(
                name=person.full_name or person.phone_number,
                email=settings.calendly_placeholder_email,
                timezone=timezone,
                text_reminder_number=person.phone_number,
            ),
            True,
        )

    # --- Outbound: app -> Calendly (called only by calendly_push_task's sweep) ---

    async def process_pending_appointment(self, appointment: Appointment) -> None:
        try:
            if appointment.status == AppointmentStatus.cancelled:
                await self._push_cancellation(appointment)
            else:
                await self._push_booking(appointment)
        except CalendlyIntegrationError as exc:
            if "already_filled" in exc.error_codes:
                # Permanent, specific to THIS appointment's exact requested time — Calendly
                # itself already has something at that slot and retrying will hit the identical
                # conflict every time. Distinct from every other failure (incl. config mistakes
                # like invalid_location_choice, which self-heal once fixed): the reconciliation
                # sweep's _unstick_failed_or_stuck() only resets "failed" rows, so this status
                # is never auto-retried — a human has to notice and rebook at a different time.
                appointment.calendly_sync_status = CalendlySyncStatus.permanently_failed
            else:
                appointment.calendly_sync_status = CalendlySyncStatus.failed
            # "Required Questions and Answers cannot be blank" has no structured error code (no
            # "details" array at all in Calendly's response, unlike already_filled/invalid_
            # location_choice) — only this exact substring to key off. This is a systemic config
            # mismatch (a required custom question added on the Event Type with no matching
            # CALENDLY_EVENT_PHONE_QUESTION answer, or the two no longer matching byte-for-byte),
            # not specific to one appointment, so it stays "failed"/auto-retried, not
            # "permanently_failed" — fixing the config should let every blocked row succeed again.
            # The enrichment below just makes the real cause obvious from calendly_sync_error
            # alone, rather than requiring a trip through the worker logs to diagnose again.
            error_message = str(exc)
            if "Questions and Answers cannot be blank" in error_message:
                error_message = (
                    f"{error_message} — likely cause: the Calendly Event Type has a REQUIRED "
                    "custom question with no answer supplied. Check that "
                    "CALENDLY_EVENT_PHONE_QUESTION / CALENDLY_EVENT_PHONE_QUESTION_POSITION in "
                    ".env exactly match that Event Type's own custom_questions[].name/.position "
                    "(GET /event_types/<id>), or that a newly added required question also has "
                    "handling added here."
                )
            appointment.calendly_sync_error = error_message
            await appointment.save()
            logger.error(
                "calendly_push_failed",
                appointment_id=str(appointment.id),
                error=error_message,
                permanent=appointment.calendly_sync_status == CalendlySyncStatus.permanently_failed,
            )

    async def _push_cancellation(self, appointment: Appointment) -> None:
        if appointment.calendly_event_uri is None:
            # Never actually reached Calendly (push never completed, or a legacy pre-integration
            # row) — nothing to cancel there.
            appointment.calendly_sync_status = CalendlySyncStatus.not_applicable
            await appointment.save()
            return

        await calendly_client.cancel_scheduled_event(
            _event_uuid_from_uri(appointment.calendly_event_uri), reason="Cancelled via Booking Agent"
        )
        appointment.calendly_sync_status = CalendlySyncStatus.synced
        appointment.calendly_sync_error = None
        appointment.calendly_last_synced_at = utcnow()
        await appointment.save()
        logger.info("calendly_cancellation_pushed", appointment_id=str(appointment.id))

    async def _push_booking(self, appointment: Appointment) -> None:
        if appointment.calendly_invitee_uri is not None:
            # Already pushed on an earlier tick (e.g. this row was re-queued) — don't double-create.
            appointment.calendly_sync_status = CalendlySyncStatus.synced
            await appointment.save()
            return
        if not settings.calendly_event_type_uri:
            raise CalendlyIntegrationError("CALENDLY_EVENT_TYPE_URI is not configured")

        person = await person_repository.get_by_id(appointment.person_id)
        if person is None:
            raise CalendlyIntegrationError(f"Person {appointment.person_id} not found for Calendly push")

        appointment.calendly_sync_status = CalendlySyncStatus.syncing
        await appointment.save()

        timezone = await self._resolve_timezone()
        invitee_input, is_placeholder = self._build_invitee_input(person, timezone)

        location = None
        if settings.calendly_event_location_kind:
            location = CalendlyLocationInput(
                kind=settings.calendly_event_location_kind,
                location=settings.calendly_event_location_text,
            )

        questions_and_answers = None
        if settings.calendly_event_phone_question:
            # Answers the Event Type's own REQUIRED phone-number question with the same value
            # already sent via text_reminder_number — see CALENDLY_EVENT_PHONE_QUESTION's config
            # comment for why this is unconditionally required by Calendly whenever the Event
            # Type has any required custom question with no matching answer supplied.
            questions_and_answers = [
                CalendlyQuestionAndAnswer(
                    question=settings.calendly_event_phone_question,
                    answer=person.phone_number,
                    position=settings.calendly_event_phone_question_position,
                )
            ]

        response = await calendly_client.create_invitee(
            CalendlyCreateInviteeRequest(
                event_type=settings.calendly_event_type_uri,
                start_time=ensure_utc(appointment.appointment_datetime).isoformat(),
                invitee=invitee_input,
                location=location,
                questions_and_answers=questions_and_answers,
            )
        )
        appointment.calendly_invitee_uri = response.resource.uri
        appointment.calendly_event_uri = response.resource.event
        appointment.is_placeholder_email = is_placeholder
        appointment.calendly_sync_status = CalendlySyncStatus.synced
        appointment.calendly_sync_error = None
        appointment.calendly_last_synced_at = utcnow()
        await appointment.save()
        logger.info(
            "calendly_booking_pushed",
            appointment_id=str(appointment.id),
            invitee_uri=response.resource.uri,
            is_placeholder_email=is_placeholder,
        )

    # --- Inbound: Calendly -> app (webhook-driven, and reused by the reconciliation sweep) ---

    async def handle_webhook_event(self, event: CalendlyWebhookEvent) -> None:
        """Never raises — a transient Calendly API failure partway through handling (e.g. the
        get_scheduled_event lookup below) must not surface as a failed delivery back to Calendly,
        which could cause it to retry-bomb us or back off the subscription entirely. Worst case,
        this specific event is dropped; workers/tasks/calendly_reconciliation_task.py's sweep
        independently re-derives the same end state from a direct poll, so nothing is silently
        lost forever — just delayed up to its own interval.
        """
        try:
            if event.event == "invitee.created":
                await self._handle_invitee_created(event.payload)
            elif event.event == "invitee.canceled":
                await self._handle_invitee_canceled(event.payload)
            # parse_webhook_event already filtered anything else to None before this is reached.
        except CalendlyIntegrationError:
            logger.exception("calendly_webhook_handling_failed", event=event.event, invitee_uri=event.payload.uri)

    async def _resolve_person(self, payload: CalendlyWebhookInviteePayload) -> Person | None:
        if payload.text_reminder_number:
            return await person_repository.get_or_create_by_phone(
                payload.text_reminder_number, full_name=payload.name
            )
        # Fallback: a direct-Calendly booking's phone is often only collected via a custom
        # question (CALENDLY_EVENT_PHONE_QUESTION), not Calendly's native SMS-reminder field —
        # only that native field populates text_reminder_number above. Confirmed live 2026-10-09
        # that questions_and_answers[].question is an exact match of the Event Type's own
        # custom_questions[].name, same byte-for-byte requirement as the outbound push side.
        if settings.calendly_event_phone_question and payload.questions_and_answers:
            for qa in payload.questions_and_answers:
                if qa.question == settings.calendly_event_phone_question and qa.answer:
                    return await person_repository.get_or_create_by_phone(qa.answer, full_name=payload.name)
        if payload.email:
            person = await person_repository.get_by_email(payload.email)
            if person is not None:
                return person
        logger.warning(
            "calendly_webhook_unresolvable_person",
            invitee_uri=payload.uri,
            note="No usable phone (neither text_reminder_number nor the configured custom "
            "question answer) and no matching email — cannot resolve a Person for this "
            "Calendly-direct booking.",
        )
        return None

    async def _handle_invitee_created(self, payload: CalendlyWebhookInviteePayload) -> None:
        # Loop-prevention, tier 1: already-known URI = just an echo/confirmation of our own push.
        existing = await appointment_repository.get_by_calendly_invitee_uri(payload.uri)
        if existing is not None:
            logger.info(
                "calendly_webhook_echo_ignored", invitee_uri=payload.uri, appointment_id=str(existing.id)
            )
            return

        event_uuid = _event_uuid_from_uri(payload.event)
        event_response = await calendly_client.get_scheduled_event(event_uuid)

        # A user-scoped webhook subscription fires for every Event Type that user owns, not just
        # this integration's one — confirmed against Calendly's own webhook-subscriptions API spec,
        # there's no event_type filter available at subscription-creation time either. Mirrors the
        # same client-side filter workers/tasks/calendly_reconciliation_task.py applies to its own
        # polled events, just applied per-delivery here instead of to a batch.
        if event_response.resource.event_type != settings.calendly_event_type_uri:
            logger.info(
                "calendly_webhook_other_event_type_ignored",
                invitee_uri=payload.uri,
                event_type=event_response.resource.event_type,
            )
            return

        start_time = _parse_calendly_datetime(event_response.resource.start_time)
        end_time = _parse_calendly_datetime(event_response.resource.end_time)

        # Loop-prevention, tier 2 (race guard): our own push's response may not have landed on
        # the local row yet. Self-heal instead of creating a duplicate.
        if start_time is not None:
            candidate = await appointment_repository.get_pending_api_push_at(start_time)
            if candidate is not None:
                candidate.calendly_invitee_uri = payload.uri
                candidate.calendly_event_uri = payload.event
                candidate.calendly_sync_status = CalendlySyncStatus.synced
                candidate.calendly_last_synced_at = utcnow()
                await candidate.save()
                logger.info(
                    "calendly_webhook_race_self_healed",
                    invitee_uri=payload.uri,
                    appointment_id=str(candidate.id),
                )
                return

        if start_time is None:
            logger.warning("calendly_webhook_created_no_start_time", invitee_uri=payload.uri)
            return

        person = await self._resolve_person(payload)
        if person is None:
            return

        duration_minutes = None
        if end_time is not None:
            duration_minutes = max(1, int((end_time - start_time).total_seconds() // 60))

        # A reschedule pair's "new" half (old_invitee set) vs. a genuinely fresh direct booking.
        original_appointment_id: str | None = None
        status = AppointmentStatus.booked
        if payload.old_invitee:
            old_appointment = await appointment_repository.get_by_calendly_invitee_uri(payload.old_invitee)
            if old_appointment is not None:
                original_appointment_id = str(old_appointment.id)
                status = AppointmentStatus.rescheduled
                if old_appointment.status != AppointmentStatus.cancelled:
                    # Flip it now rather than waiting for its own invitee.canceled — delivery
                    # order between the pair isn't guaranteed. If that webhook arrives later and
                    # finds this row already cancelled, its own idempotency check no-ops cleanly.
                    old_appointment.status = AppointmentStatus.cancelled
                    old_appointment.calendly_sync_status = CalendlySyncStatus.synced
                    await old_appointment.save()
            else:
                logger.info(
                    "calendly_webhook_reschedule_old_invitee_unmatched", old_invitee=payload.old_invitee
                )

        appointment = Appointment(
            person_id=str(person.id),
            appointment_datetime=start_time,
            duration_minutes=duration_minutes,
            status=status,
            booking_source=BookingSource.calendly_direct,
            original_appointment_id=original_appointment_id,
            scheduling_method=CalendlySchedulingMethod.calendly_direct,
            calendly_invitee_uri=payload.uri,
            calendly_event_uri=payload.event,
            calendly_sync_status=CalendlySyncStatus.synced,
            calendly_last_synced_at=utcnow(),
        )
        try:
            await appointment.insert()
        except DuplicateKeyError:
            # A genuine slot conflict (something else already active at this exact time) — log
            # and skip rather than guessing; still returns 200 to Calendly either way.
            logger.warning(
                "calendly_webhook_slot_conflict", invitee_uri=payload.uri, start_time=start_time.isoformat()
            )
            return
        logger.info(
            "calendly_webhook_appointment_created", invitee_uri=payload.uri, appointment_id=str(appointment.id)
        )

    async def _handle_invitee_canceled(self, payload: CalendlyWebhookInviteePayload) -> None:
        appointment = await appointment_repository.get_by_calendly_invitee_uri(payload.uri)
        if appointment is None:
            # Nothing local to react to — e.g. a booking that predates this integration.
            logger.info("calendly_webhook_cancel_unmatched", invitee_uri=payload.uri)
            return
        if appointment.status == AppointmentStatus.cancelled:
            # Idempotent no-op — covers both a duplicate webhook delivery AND the case where we
            # already pushed this exact cancellation ourselves and this is Calendly's own echo.
            return

        appointment.status = AppointmentStatus.cancelled
        reason = (payload.cancellation or {}).get("reason") if payload.cancellation else None
        if reason:
            appointment.notes = f"{appointment.notes or ''}\nCancelled via Calendly: {reason}".strip()
        appointment.calendly_sync_status = CalendlySyncStatus.synced
        appointment.calendly_last_synced_at = utcnow()
        await appointment.save()
        logger.info(
            "calendly_webhook_appointment_cancelled", invitee_uri=payload.uri, appointment_id=str(appointment.id)
        )


calendly_sync_service = CalendlySyncService()
