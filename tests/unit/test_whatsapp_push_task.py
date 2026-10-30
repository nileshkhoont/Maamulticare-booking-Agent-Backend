"""Covers the WhatsApp push sweep's own logic (which rows it picks up, the early-exit guards) —
services/whatsapp_notification_service.py's own send logic is covered separately in
test_whatsapp_notification_service.py.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.models.appointment import Appointment
from app.models.person import Person
from app.workers.tasks.whatsapp_push_task import push_pending_whatsapp_notifications_once

_slot_counter = 0


async def _make_pending_appointment() -> Appointment:
    global _slot_counter
    _slot_counter += 1
    person = Person(phone_number=f"+91900000{_slot_counter:04d}")
    await person.insert()
    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime=datetime.now(UTC) + timedelta(days=_slot_counter),
        status="booked",
        booking_source="inbound_call",
        whatsapp_notification_type="booked",
        whatsapp_notification_status="pending",
    )
    await appointment.insert()
    return appointment


@pytest.mark.asyncio
async def test_sweep_skips_entirely_when_disabled(business_config):
    original = settings.meta_whatsapp_notifications_enabled
    settings.meta_whatsapp_notifications_enabled = False
    try:
        await _make_pending_appointment()
        processed = await push_pending_whatsapp_notifications_once()
    finally:
        settings.meta_whatsapp_notifications_enabled = original
    assert processed == 0


@pytest.mark.asyncio
async def test_sweep_skips_entirely_when_unconfigured(business_config):
    original_token = settings.meta_whatsapp_access_token
    settings.meta_whatsapp_access_token = None
    try:
        await _make_pending_appointment()
        processed = await push_pending_whatsapp_notifications_once()
    finally:
        settings.meta_whatsapp_access_token = original_token
    assert processed == 0


@pytest.mark.asyncio
async def test_sweep_processes_each_pending_row(business_config):
    await _make_pending_appointment()
    await _make_pending_appointment()

    fake_process = AsyncMock()
    with patch(
        "app.workers.tasks.whatsapp_push_task.whatsapp_notification_service.process_pending_appointment",
        fake_process,
    ):
        processed = await push_pending_whatsapp_notifications_once()

    assert processed == 2
    assert fake_process.await_count == 2


@pytest.mark.asyncio
async def test_sweep_one_bad_row_does_not_stop_the_rest(business_config):
    await _make_pending_appointment()
    await _make_pending_appointment()

    fake_process = AsyncMock(side_effect=[RuntimeError("unexpected bug"), None])
    with patch(
        "app.workers.tasks.whatsapp_push_task.whatsapp_notification_service.process_pending_appointment",
        fake_process,
    ):
        processed = await push_pending_whatsapp_notifications_once()  # must not raise

    assert processed == 2
    assert fake_process.await_count == 2
