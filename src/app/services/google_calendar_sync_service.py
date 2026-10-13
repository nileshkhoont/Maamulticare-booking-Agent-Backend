"""One-way, OUTBOUND-ONLY Google Calendar sync: mirrors our own book/reschedule/cancel actions
onto a single pre-shared Google Calendar, purely for the doctor's own visibility. There is no
inbound direction at all — patients can only book/reschedule/cancel via the Edesy AI phone system;
Google Calendar is never read back into this system. Our MongoDB stays authoritative throughout.

Outbound pushes are poll-based (workers/tasks/google_calendar_push_task.py), not fired inline from
appointment_service.py, matching this codebase's existing convention of never calling an external
integration synchronously from an admin-facing request.

Unlike a two-way sync, reschedule needs NO special pairing logic: appointment_service.
reschedule_existing already creates a brand-new Appointment row (status=rescheduled) and
independently cancels the old one (status=cancelled) as two separate rows — each just goes
through this same generic pending-sync queue on its own: the old row's cancel deletes its Google
event, the new row's create makes a fresh one.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.core.constants import AppointmentStatus, GoogleCalendarSyncStatus
from app.core.exceptions import GoogleCalendarIntegrationError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.integrations.google_calendar.client import google_calendar_client
from app.integrations.google_calendar.schemas import GoogleCalendarEvent, GoogleCalendarEventDateTime
from app.models.appointment import Appointment
from app.models.business_config import BusinessConfig
from app.models.person import Person
from app.repositories.person_repository import person_repository
from app.utils.datetime_utils import ensure_utc

logger = get_logger(__name__)

_DEFAULT_TIMEZONE = "Asia/Kolkata"  # fallback only — see _resolve_timezone


class GoogleCalendarSyncService:
    async def _resolve_timezone(self) -> str:
        config = await BusinessConfig.find_one({})
        return config.timezone if config and config.timezone else _DEFAULT_TIMEZONE

    def _resolve_duration_minutes(self, appointment: Appointment) -> int:
        return appointment.duration_minutes or settings.google_calendar_default_event_duration_minutes

    def _build_event_datetime(self, dt_utc, timezone: str) -> GoogleCalendarEventDateTime:
        try:
            localized = dt_utc.astimezone(ZoneInfo(timezone))
        except ZoneInfoNotFoundError:
            logger.warning("google_calendar_invalid_timezone", timezone=timezone)
            timezone = _DEFAULT_TIMEZONE
            localized = dt_utc.astimezone(ZoneInfo(timezone))
        return GoogleCalendarEventDateTime(dateTime=localized.isoformat(), timeZone=timezone)

    def _build_description(self, person: Person, appointment: Appointment) -> str:
        lines = [f"Patient: {person.full_name or 'Unknown'}", f"Phone: {person.phone_number}"]
        if appointment.notes:
            lines.append(f"Notes: {appointment.notes}")
        lines.append("Booked via the Edesy AI phone system — do not edit this event directly.")
        return "\n".join(lines)

    # --- Outbound: app -> Google Calendar (called only by google_calendar_push_task's sweep) ---

    async def process_pending_appointment(self, appointment: Appointment) -> None:
        try:
            if appointment.status == AppointmentStatus.cancelled:
                await self._push_cancellation(appointment)
            else:
                await self._push_booking(appointment)
        except GoogleCalendarIntegrationError as exc:
            appointment.google_calendar_sync_status = GoogleCalendarSyncStatus.failed
            appointment.google_calendar_sync_error = str(exc)
            await appointment.save()
            logger.error("google_calendar_push_failed", appointment_id=str(appointment.id), error=str(exc))

    async def _push_cancellation(self, appointment: Appointment) -> None:
        if appointment.google_calendar_event_id is None:
            # Never actually reached Google Calendar (push never completed, or a legacy
            # pre-integration row) — nothing to delete there.
            appointment.google_calendar_sync_status = GoogleCalendarSyncStatus.not_applicable
            await appointment.save()
            return

        await google_calendar_client.delete_event(appointment.google_calendar_event_id)
        appointment.google_calendar_sync_status = GoogleCalendarSyncStatus.synced
        appointment.google_calendar_sync_error = None
        appointment.google_calendar_last_synced_at = utcnow()
        await appointment.save()
        logger.info(
            "google_calendar_event_deleted",
            appointment_id=str(appointment.id),
            event_id=appointment.google_calendar_event_id,
        )

    async def _push_booking(self, appointment: Appointment) -> None:
        if appointment.google_calendar_event_id is not None:
            # Already pushed on an earlier tick (e.g. this row was re-queued) — don't double-create.
            appointment.google_calendar_sync_status = GoogleCalendarSyncStatus.synced
            await appointment.save()
            return
        if not settings.google_calendar_id:
            raise GoogleCalendarIntegrationError("GOOGLE_CALENDAR_ID is not configured")

        person = await person_repository.get_by_id(appointment.person_id)
        if person is None:
            raise GoogleCalendarIntegrationError(
                f"Person {appointment.person_id} not found for Google Calendar push"
            )

        appointment.google_calendar_sync_status = GoogleCalendarSyncStatus.syncing
        await appointment.save()

        timezone = await self._resolve_timezone()
        start_utc = ensure_utc(appointment.appointment_datetime)
        end_utc = start_utc + timedelta(minutes=self._resolve_duration_minutes(appointment))

        event = GoogleCalendarEvent(
            # Deterministic — debugging/correlation value only, not relied on for correctness
            # (Google doesn't guarantee collision detection on a client-supplied id). A Mongo
            # ObjectId's string form is 24 lowercase hex chars, already a valid subset of the
            # required base32hex event-id charset (a-v, 0-9), 5-1024 length — no encoding needed.
            id=str(appointment.id),
            summary=f"Appointment: {person.full_name or person.phone_number}",
            description=self._build_description(person, appointment),
            location=settings.google_calendar_event_location,
            start=self._build_event_datetime(start_utc, timezone),
            end=self._build_event_datetime(end_utc, timezone),
        )
        created = await google_calendar_client.create_event(event)
        appointment.google_calendar_event_id = created.id
        appointment.google_calendar_sync_status = GoogleCalendarSyncStatus.synced
        appointment.google_calendar_sync_error = None
        appointment.google_calendar_last_synced_at = utcnow()
        await appointment.save()
        logger.info(
            "google_calendar_event_created", appointment_id=str(appointment.id), event_id=created.id
        )


google_calendar_sync_service = GoogleCalendarSyncService()
