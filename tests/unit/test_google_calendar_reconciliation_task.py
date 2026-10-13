"""Covers the Google Calendar reconciliation sweep — the "unstick failed/stuck" half only. There
is no inbound-catchup half at all for this one-way, outbound-only integration.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.appointment import Appointment
from app.models.person import Person
from app.workers.tasks.google_calendar_reconciliation_task import (
    STUCK_SYNCING_THRESHOLD_MINUTES,
    reconcile_google_calendar_once,
)

_slot_counter = 0


async def _make_appointment(phone: str, sync_status: str, **overrides) -> Appointment:
    global _slot_counter
    _slot_counter += 1
    person = Person(full_name="Test", phone_number=phone)
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime=f"2026-11-01T05:{_slot_counter:02d}:00+00:00",
        status="booked",
        booking_source="admin_scheduled_call",
        google_calendar_sync_status=sync_status,
        **overrides,
    )
    await appointment.insert()
    return appointment


@pytest.mark.asyncio
async def test_failed_appointment_unstuck_to_pending(business_config):
    appointment = await _make_appointment("+917600181450", "failed")
    processed = await reconcile_google_calendar_once()
    assert processed == 1
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.google_calendar_sync_status == "pending"


@pytest.mark.asyncio
async def test_stuck_syncing_past_threshold_unstuck_to_pending(business_config):
    appointment = await _make_appointment("+917600181451", "syncing")
    stale_cutoff = datetime.now(UTC) - timedelta(minutes=STUCK_SYNCING_THRESHOLD_MINUTES + 1)
    appointment.updated_at = stale_cutoff
    await appointment.save()

    processed = await reconcile_google_calendar_once()
    assert processed == 1
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.google_calendar_sync_status == "pending"


@pytest.mark.asyncio
async def test_recently_updated_syncing_row_left_alone(business_config):
    await _make_appointment("+917600181452", "syncing")  # updated_at defaults to now
    processed = await reconcile_google_calendar_once()
    assert processed == 0


@pytest.mark.asyncio
async def test_synced_and_not_applicable_rows_never_touched(business_config):
    await _make_appointment("+917600181453", "synced")
    await _make_appointment("+917600181454", "not_applicable")
    processed = await reconcile_google_calendar_once()
    assert processed == 0
