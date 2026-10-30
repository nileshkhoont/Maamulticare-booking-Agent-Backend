from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.core.constants import WhatsAppNotificationStatus, WhatsAppNotificationType
from app.integrations.whatsapp.schemas import WhatsAppSendResult
from app.models.appointment import Appointment
from app.models.business_config import BusinessConfig
from app.models.person import Person
from app.services.whatsapp_notification_service import whatsapp_notification_service


async def _make_person(phone: str, full_name: str | None = "Priya Shah") -> Person:
    person = Person(full_name=full_name, phone_number=phone)
    await person.insert()
    return person


async def _make_appointment(
    person_id: str, notification_type: WhatsAppNotificationType, **overrides
) -> Appointment:
    fields = {
        "person_id": person_id,
        "appointment_datetime": datetime.now(UTC) + timedelta(days=1),
        "status": "booked",
        "booking_source": "inbound_call",
        "whatsapp_notification_type": notification_type,
        "whatsapp_notification_status": WhatsAppNotificationStatus.pending,
    }
    fields.update(overrides)
    appointment = Appointment(**fields)
    await appointment.insert()
    return appointment


@pytest.mark.asyncio
async def test_process_pending_booked_sends_expected_template(business_config: BusinessConfig):
    person = await _make_person("+919876543210")
    appointment = await _make_appointment(str(person.id), WhatsAppNotificationType.booked)

    fake_send = AsyncMock(return_value=WhatsAppSendResult(message_id="wamid.ABC"))
    with patch("app.services.whatsapp_notification_service.whatsapp_client.send_template_message", fake_send):
        await whatsapp_notification_service.process_pending_appointment(appointment)

    fake_send.assert_awaited_once()
    call_kwargs = fake_send.call_args.kwargs
    assert call_kwargs["template_name"] == "appointment_booked_v1"
    assert call_kwargs["to"] == "919876543210"  # leading "+" stripped
    assert call_kwargs["body_parameters"][0] == "Priya Shah"

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_message_id == "wamid.ABC"
    assert refreshed.whatsapp_notification_status == "sent"
    assert refreshed.whatsapp_notification_attempt_count == 1


@pytest.mark.asyncio
async def test_process_pending_rescheduled_uses_rescheduled_template(business_config: BusinessConfig):
    person = await _make_person("+919876543211")
    appointment = await _make_appointment(str(person.id), WhatsAppNotificationType.rescheduled)

    fake_send = AsyncMock(return_value=WhatsAppSendResult(message_id="wamid.DEF"))
    with patch("app.services.whatsapp_notification_service.whatsapp_client.send_template_message", fake_send):
        await whatsapp_notification_service.process_pending_appointment(appointment)

    assert fake_send.call_args.kwargs["template_name"] == "appointment_rescheduled_v1"


@pytest.mark.asyncio
async def test_process_pending_cancelled_uses_cancelled_template(business_config: BusinessConfig):
    person = await _make_person("+919876543212")
    appointment = await _make_appointment(str(person.id), WhatsAppNotificationType.cancelled)

    fake_send = AsyncMock(return_value=WhatsAppSendResult(message_id="wamid.GHI"))
    with patch("app.services.whatsapp_notification_service.whatsapp_client.send_template_message", fake_send):
        await whatsapp_notification_service.process_pending_appointment(appointment)

    assert fake_send.call_args.kwargs["template_name"] == "appointment_cancelled_v1"


@pytest.mark.asyncio
async def test_process_pending_falls_back_to_phone_when_name_missing(business_config: BusinessConfig):
    person = await _make_person("+919876543213", full_name=None)
    appointment = await _make_appointment(str(person.id), WhatsAppNotificationType.booked)

    fake_send = AsyncMock(return_value=WhatsAppSendResult(message_id="wamid.JKL"))
    with patch("app.services.whatsapp_notification_service.whatsapp_client.send_template_message", fake_send):
        await whatsapp_notification_service.process_pending_appointment(appointment)

    assert fake_send.call_args.kwargs["body_parameters"][0] == "+919876543213"


@pytest.mark.asyncio
async def test_process_pending_invalid_timezone_falls_back(business_config: BusinessConfig):
    business_config.timezone = "Not/A_Real_Zone"
    await business_config.save()
    person = await _make_person("+919876543214")
    appointment = await _make_appointment(str(person.id), WhatsAppNotificationType.booked)

    fake_send = AsyncMock(return_value=WhatsAppSendResult(message_id="wamid.MNO"))
    with patch("app.services.whatsapp_notification_service.whatsapp_client.send_template_message", fake_send):
        await whatsapp_notification_service.process_pending_appointment(appointment)  # must not raise

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "sent"


@pytest.mark.asyncio
async def test_process_pending_skips_if_already_sent(business_config: BusinessConfig):
    person = await _make_person("+919876543215")
    appointment = await _make_appointment(
        str(person.id), WhatsAppNotificationType.booked, whatsapp_message_id="wamid.ALREADY"
    )

    fake_send = AsyncMock()
    with patch("app.services.whatsapp_notification_service.whatsapp_client.send_template_message", fake_send):
        await whatsapp_notification_service.process_pending_appointment(appointment)

    fake_send.assert_not_awaited()
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "sent"


@pytest.mark.asyncio
async def test_process_pending_missing_person_marks_failed_without_raising(business_config: BusinessConfig):
    appointment = await _make_appointment("does-not-exist", WhatsAppNotificationType.booked)

    await whatsapp_notification_service.process_pending_appointment(appointment)  # must not raise

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "failed"
    assert refreshed.whatsapp_notification_error is not None


@pytest.mark.asyncio
async def test_process_pending_client_error_marks_failed_without_raising(business_config: BusinessConfig):
    from app.core.exceptions import WhatsAppIntegrationError

    person = await _make_person("+919876543216")
    appointment = await _make_appointment(str(person.id), WhatsAppNotificationType.booked)

    fake_send = AsyncMock(side_effect=WhatsAppIntegrationError("boom"))
    with patch("app.services.whatsapp_notification_service.whatsapp_client.send_template_message", fake_send):
        await whatsapp_notification_service.process_pending_appointment(appointment)  # must not raise

    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "failed"
    assert refreshed.whatsapp_notification_error == "boom"
    assert refreshed.whatsapp_notification_attempt_count == 1


@pytest.mark.asyncio
async def test_status_webhook_unknown_message_id_is_a_noop():
    await whatsapp_notification_service.process_status_webhook_event(
        message_id="wamid.UNKNOWN", new_status="delivered", error_detail=None
    )  # must not raise


@pytest.mark.asyncio
async def test_status_webhook_applies_forward_progress(business_config: BusinessConfig):
    person = await _make_person("+919876543217")
    appointment = await _make_appointment(
        str(person.id),
        WhatsAppNotificationType.booked,
        whatsapp_message_id="wamid.PROGRESS",
        whatsapp_notification_status=WhatsAppNotificationStatus.sent,
    )

    await whatsapp_notification_service.process_status_webhook_event(
        message_id="wamid.PROGRESS", new_status="delivered", error_detail=None
    )
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "delivered"


@pytest.mark.asyncio
async def test_status_webhook_ignores_lower_rank_redelivery(business_config: BusinessConfig):
    person = await _make_person("+919876543218")
    appointment = await _make_appointment(
        str(person.id),
        WhatsAppNotificationType.booked,
        whatsapp_message_id="wamid.READ",
        whatsapp_notification_status=WhatsAppNotificationStatus.read,
    )

    await whatsapp_notification_service.process_status_webhook_event(
        message_id="wamid.READ", new_status="delivered", error_detail=None
    )
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "read"  # unchanged — not a regression


@pytest.mark.asyncio
async def test_status_webhook_failed_always_applies_regardless_of_rank(business_config: BusinessConfig):
    person = await _make_person("+919876543219")
    appointment = await _make_appointment(
        str(person.id),
        WhatsAppNotificationType.booked,
        whatsapp_message_id="wamid.FAILCASE",
        whatsapp_notification_status=WhatsAppNotificationStatus.read,
    )

    await whatsapp_notification_service.process_status_webhook_event(
        message_id="wamid.FAILCASE", new_status="failed", error_detail="Recipient unreachable"
    )
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "failed"
    assert refreshed.whatsapp_notification_error == "Recipient unreachable"
