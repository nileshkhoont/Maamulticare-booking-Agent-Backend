"""Covers the WhatsApp reconciliation sweep — unlike Google Calendar's, `failed` rows are only
retried a bounded number of times (MAX_WHATSAPP_SEND_ATTEMPTS), not forever. There is no
inbound-catchup half at all for this one-way, outbound-only integration.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.appointment import Appointment
from app.models.person import Person
from app.workers.tasks.whatsapp_reconciliation_task import (
    MAX_WHATSAPP_SEND_ATTEMPTS,
    STUCK_SENDING_THRESHOLD_MINUTES,
    reconcile_whatsapp_notifications_once,
)

_slot_counter = 0


async def _make_appointment(status: str, **overrides) -> Appointment:
    global _slot_counter
    _slot_counter += 1
    person = Person(phone_number=f"+91910000{_slot_counter:04d}")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime=f"2026-11-02T05:{_slot_counter:02d}:00+00:00",
        status="booked",
        booking_source="inbound_call",
        whatsapp_notification_type="booked",
        whatsapp_notification_status=status,
        **overrides,
    )
    await appointment.insert()
    return appointment


@pytest.mark.asyncio
async def test_failed_under_attempt_cap_unstuck_to_pending(business_config):
    appointment = await _make_appointment("failed", whatsapp_notification_attempt_count=1)
    processed = await reconcile_whatsapp_notifications_once()
    assert processed == 1
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "pending"


@pytest.mark.asyncio
async def test_failed_at_attempt_cap_left_alone(business_config):
    await _make_appointment("failed", whatsapp_notification_attempt_count=MAX_WHATSAPP_SEND_ATTEMPTS)
    processed = await reconcile_whatsapp_notifications_once()
    assert processed == 0


@pytest.mark.asyncio
async def test_stuck_sending_past_threshold_unstuck_to_pending(business_config):
    appointment = await _make_appointment("sending", whatsapp_notification_attempt_count=1)
    stale_cutoff = datetime.now(UTC) - timedelta(minutes=STUCK_SENDING_THRESHOLD_MINUTES + 1)
    appointment.updated_at = stale_cutoff
    await appointment.save()

    processed = await reconcile_whatsapp_notifications_once()
    assert processed == 1
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "pending"


@pytest.mark.asyncio
async def test_recently_updated_sending_row_left_alone(business_config):
    await _make_appointment("sending", whatsapp_notification_attempt_count=1)  # updated_at defaults to now
    processed = await reconcile_whatsapp_notifications_once()
    assert processed == 0


@pytest.mark.asyncio
async def test_sent_delivered_read_rows_never_touched(business_config):
    await _make_appointment("sent")
    await _make_appointment("delivered")
    await _make_appointment("read")
    processed = await reconcile_whatsapp_notifications_once()
    assert processed == 0
