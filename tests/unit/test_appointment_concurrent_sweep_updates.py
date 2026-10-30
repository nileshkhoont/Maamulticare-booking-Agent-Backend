"""Regression test for the 2026-10-15 incident: google_calendar_push_task and whatsapp_push_task
are two independent Celery sweeps that can both pick up the SAME freshly-booked appointment within
the same ~30s window, each loading its own in-memory copy of the document. The original code had
both services call `appointment.save()`, which writes the document's ENTIRE in-memory field set —
so whichever sweep saved last silently overwrote the other's concurrent updates with its own stale
snapshot. Observed effect in production: whatsapp_notification_status got reverted from "sent"
back to "pending" by a concurrent google_calendar_push_task save, so the next whatsapp sweep tick
saw "pending" again and sent a second WhatsApp message to the same patient.

Fixed by switching every write in both services (and the two reconciliation tasks, and the other
Appointment-mutating call sites that run in this same post-booking window) to Beanie's atomic
`Set` operator — a MongoDB $set touching only the named fields, which can never clobber a field
it doesn't mention. This test proves that directly: it simulates the exact race (two independently
loaded, genuinely stale copies of the same row, each updated by a different service) and asserts
neither one's write is lost.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.integrations.google_calendar.schemas import GoogleCalendarEvent
from app.integrations.whatsapp.schemas import WhatsAppSendResult
from app.models.appointment import Appointment
from app.models.person import Person
from app.services.google_calendar_sync_service import google_calendar_sync_service
from app.services.whatsapp_notification_service import whatsapp_notification_service


@pytest.mark.asyncio
async def test_concurrent_google_calendar_and_whatsapp_sweeps_do_not_clobber_each_other(business_config):
    person = Person(full_name="Dhaval", phone_number="+919328525350")
    await person.insert()

    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime=datetime.now(UTC) + timedelta(days=1),
        status="booked",
        booking_source="inbound_call",
        google_calendar_sync_status="pending",
        whatsapp_notification_type="booked",
        whatsapp_notification_status="pending",
    )
    await appointment.insert()

    # Simulate two Celery tasks (concurrency=4 prefork) each independently fetching their OWN
    # in-memory copy of the exact same row — exactly what google_calendar_push_task and
    # whatsapp_push_task do in production, one tick apart or truly concurrently.
    copy_for_calendar = await Appointment.get(appointment.id)
    copy_for_whatsapp = await Appointment.get(appointment.id)

    fake_create_event = AsyncMock(return_value=GoogleCalendarEvent(id="evt-concurrent-1"))
    with patch(
        "app.services.google_calendar_sync_service.google_calendar_client.create_event", fake_create_event
    ):
        await google_calendar_sync_service.process_pending_appointment(copy_for_calendar)

    # copy_for_whatsapp's OWN snapshot of google_calendar_sync_status is still "pending" — taken
    # BEFORE the calendar sweep above ran. The bug: a plain .save() here would write that stale
    # "pending" back to the DB, undoing the calendar sweep's update.
    fake_send = AsyncMock(return_value=WhatsAppSendResult(message_id="wamid-concurrent-1"))
    with patch("app.services.whatsapp_notification_service.whatsapp_client.send_template_message", fake_send):
        await whatsapp_notification_service.process_pending_appointment(copy_for_whatsapp)

    final = await Appointment.get(appointment.id)
    # Both sweeps' updates must have landed — neither clobbered the other's fields.
    assert final.google_calendar_sync_status == "synced"
    assert final.google_calendar_event_id == "evt-concurrent-1"
    assert final.whatsapp_notification_status == "sent"
    assert final.whatsapp_message_id == "wamid-concurrent-1"


@pytest.mark.asyncio
async def test_reverse_order_also_does_not_clobber(business_config):
    """Same race, opposite finishing order — proves the fix isn't order-dependent."""
    person = Person(full_name="Priya", phone_number="+919876543299")
    await person.insert()

    appointment = Appointment(
        person_id=str(person.id),
        appointment_datetime=datetime.now(UTC) + timedelta(days=1),
        status="booked",
        booking_source="inbound_call",
        google_calendar_sync_status="pending",
        whatsapp_notification_type="booked",
        whatsapp_notification_status="pending",
    )
    await appointment.insert()

    copy_for_whatsapp = await Appointment.get(appointment.id)
    copy_for_calendar = await Appointment.get(appointment.id)

    fake_send = AsyncMock(return_value=WhatsAppSendResult(message_id="wamid-concurrent-2"))
    with patch("app.services.whatsapp_notification_service.whatsapp_client.send_template_message", fake_send):
        await whatsapp_notification_service.process_pending_appointment(copy_for_whatsapp)

    fake_create_event = AsyncMock(return_value=GoogleCalendarEvent(id="evt-concurrent-2"))
    with patch(
        "app.services.google_calendar_sync_service.google_calendar_client.create_event", fake_create_event
    ):
        await google_calendar_sync_service.process_pending_appointment(copy_for_calendar)

    final = await Appointment.get(appointment.id)
    assert final.whatsapp_notification_status == "sent"
    assert final.whatsapp_message_id == "wamid-concurrent-2"
    assert final.google_calendar_sync_status == "synced"
    assert final.google_calendar_event_id == "evt-concurrent-2"
