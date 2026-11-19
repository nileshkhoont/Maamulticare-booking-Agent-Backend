"""Characterization tests: pin down how the CURRENT end-to-end flows behave, so the reliability
fixes (fix/queue-and-appointment-reliability) can be proven not to change any of them. Every test
in this file was run against the pre-fix code first and passed there, unchanged.

Covers: outbound dispatch -> Edesy -> call.ended webhook, the stuck-schedule sweep's legacy
behaviour, the agent's book -> reschedule -> cancel flow, and the WhatsApp + Google Calendar
notification lifecycles.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.exceptions import EdesyIntegrationError
from app.integrations.edesy.schemas import EdesyPlaceCallResponse
from app.integrations.google_calendar.schemas import GoogleCalendarEvent
from app.integrations.whatsapp.schemas import WhatsAppSendResult
from app.models.appointment import Appointment
from app.models.call import Call
from app.models.call_schedule import CallSchedule
from app.models.person import Person
from app.services.appointment_service import appointment_service
from app.workers.tasks.google_calendar_push_task import push_pending_google_calendar_appointments_once
from app.workers.tasks.missed_call_retry_task import sweep_stuck_schedules_once
from app.workers.tasks.outbound_call_task import dispatch_due_calls_once
from app.workers.tasks.whatsapp_push_task import push_pending_whatsapp_notifications_once

TOOL_HEADERS = {"X-Tool-Secret": settings.agent_tool_secret}
PLACE_CALL = "app.workers.tasks.outbound_call_task.edesy_client.place_call"
SEND = "app.services.whatsapp_notification_service.whatsapp_client.send_template_message"
CREATE_EVENT = "app.services.google_calendar_sync_service.google_calendar_client.create_event"
DELETE_EVENT = "app.services.google_calendar_sync_service.google_calendar_client.delete_event"


@pytest.fixture
def edesy_agent(monkeypatch):
    monkeypatch.setattr(settings, "edesy_agent_id", "agent-test")


def _next_weekday_at(weekday: int, hour: int, minute: int = 0) -> datetime:
    now = datetime.now(UTC)
    days_ahead = (weekday - now.weekday()) % 7 or 7
    return (now + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)


async def _person(phone: str, name: str = "Test Person") -> Person:
    person = Person(phone_number=phone, full_name=name)
    await person.insert()
    return person


async def _due_schedule(person: Person, **overrides) -> CallSchedule:
    fields = dict(
        person_id=str(person.id),
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        call_purpose="admin_scheduled",
        requested_by="admin",
        status="pending",
        admin_instructions="Remind about fasting",
    )
    fields.update(overrides)
    schedule = CallSchedule(**fields)
    await schedule.insert()
    return schedule


async def _post_edesy_webhook(client: AsyncClient, call_sid: str, phone: str, status: str, disposition):
    payload = {
        "event": "call.ended",
        "timestamp": datetime.now(UTC).isoformat(),
        "call": {"callSid": call_sid, "direction": "outbound", "from": phone, "duration": 60},
        "outcome": {"status": status, "disposition": disposition, "endReason": "USER_REQUEST"},
        "transcript": [{"speaker": "agent", "text": "Hello!"}],
    }
    return await client.post(
        "/api/v1/webhooks/edesy",
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.edesy_webhook_secret}"},
    )


# --- Outbound call queue -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_places_due_call_with_expected_payload(edesy_agent, business_config):
    person = await _person("+919800000001", "Asha")
    schedule = await _due_schedule(person)

    place_call = AsyncMock(return_value=EdesyPlaceCallResponse(call_id="sid-dispatch-1"))
    with patch(PLACE_CALL, place_call):
        dispatched = await dispatch_due_calls_once()

    assert dispatched == 1
    place_call.assert_awaited_once()
    kwargs = place_call.call_args.kwargs
    assert kwargs["agent_id"] == "agent-test"
    assert kwargs["phone_number"] == "+919800000001"
    assert kwargs["from_number"] == settings.edesy_from_number
    assert kwargs["idempotency_key"] == str(schedule.id)
    assert kwargs["context"] == {
        "call_schedule_id": str(schedule.id),
        "person_id": str(person.id),
        "appointment_id": None,
        "admin_instructions": "Admin instructions for this call: Remind about fasting",
    }
    assert kwargs["variables"] == {
        "person_name": "Asha",
        "phone_number": "+919800000001",
        "previous_appointment_status": "none",
        "previous_appointment_datetime_ist": "",
    }

    refreshed = await CallSchedule.get(schedule.id)
    assert refreshed.status == "in_progress"
    assert refreshed.edesy_call_id == "sid-dispatch-1"


@pytest.mark.asyncio
async def test_dispatch_skips_schedules_not_yet_due(edesy_agent, business_config):
    person = await _person("+919800000002")
    schedule = await _due_schedule(person, scheduled_at=datetime.now(UTC) + timedelta(hours=1))

    place_call = AsyncMock(return_value=EdesyPlaceCallResponse(call_id="sid-x"))
    with patch(PLACE_CALL, place_call):
        assert await dispatch_due_calls_once() == 0

    place_call.assert_not_awaited()
    assert (await CallSchedule.get(schedule.id)).status == "pending"


@pytest.mark.asyncio
async def test_dispatch_failure_marks_schedule_missed(edesy_agent, business_config):
    person = await _person("+919800000003")
    schedule = await _due_schedule(person)

    with patch(PLACE_CALL, AsyncMock(side_effect=EdesyIntegrationError("boom"))):
        assert await dispatch_due_calls_once() == 0

    refreshed = await CallSchedule.get(schedule.id)
    assert refreshed.status == "missed"
    assert refreshed.edesy_call_id is None


@pytest.mark.asyncio
async def test_dispatch_cancels_schedule_for_deleted_person(edesy_agent, business_config):
    person = await _person("+919800000004")
    person.is_deleted = True
    await person.save()
    schedule = await _due_schedule(person)

    place_call = AsyncMock(return_value=EdesyPlaceCallResponse(call_id="sid-x"))
    with patch(PLACE_CALL, place_call):
        await dispatch_due_calls_once()

    place_call.assert_not_awaited()
    assert (await CallSchedule.get(schedule.id)).status == "cancelled"


@pytest.mark.asyncio
async def test_dispatch_then_answered_webhook_completes_schedule(
    client: AsyncClient, edesy_agent, business_config
):
    person = await _person("+919800000005", "Ravi")
    schedule = await _due_schedule(person)
    with patch(PLACE_CALL, AsyncMock(return_value=EdesyPlaceCallResponse(call_id="sid-e2e-1"))):
        await dispatch_due_calls_once()

    response = await _post_edesy_webhook(client, "sid-e2e-1", "+919800000005", "completed", "APPOINTMENT_BOOKED")
    assert response.status_code == 200

    assert (await CallSchedule.get(schedule.id)).status == "completed"
    call = await Call.find_one(Call.edesy_call_id == "sid-e2e-1")
    assert call.call_type == "outbound_admin_scheduled"
    assert call.direction == "outbound"
    assert call.call_schedule_id == str(schedule.id)
    assert call.person_id == str(person.id)
    assert call.call_status == "answered"


@pytest.mark.asyncio
async def test_dispatch_then_no_answer_webhook_marks_schedule_missed(
    client: AsyncClient, edesy_agent, business_config
):
    person = await _person("+919800000006")
    schedule = await _due_schedule(person)
    with patch(PLACE_CALL, AsyncMock(return_value=EdesyPlaceCallResponse(call_id="sid-e2e-2"))):
        await dispatch_due_calls_once()

    await _post_edesy_webhook(client, "sid-e2e-2", "+919800000006", "no_answer", None)

    assert (await CallSchedule.get(schedule.id)).status == "missed"


@pytest.mark.asyncio
async def test_sweep_marks_long_stuck_in_progress_schedule_missed(business_config):
    """A schedule in_progress for hours with no webhook ever arriving is still swept to missed."""
    person = await _person("+919800000007")
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    schedule = await _due_schedule(person, status="in_progress", created_at=long_ago, updated_at=long_ago)

    assert await sweep_stuck_schedules_once() == 1
    assert (await CallSchedule.get(schedule.id)).status == "missed"


@pytest.mark.asyncio
async def test_sweep_leaves_recent_in_progress_schedule_alone(business_config):
    person = await _person("+919800000008")
    schedule = await _due_schedule(person, status="in_progress")

    assert await sweep_stuck_schedules_once() == 0
    assert (await CallSchedule.get(schedule.id)).status == "in_progress"


# --- Agent tools: book -> reschedule -> cancel ----------------------------------------------


@pytest.mark.asyncio
async def test_agent_book_reschedule_cancel_flow(client: AsyncClient, business_config):
    phone = "+919800000010"
    first = _next_weekday_at(0, 9).isoformat()
    second = _next_weekday_at(0, 10).isoformat()

    booked = (
        await client.post(
            "/api/v1/agent-tools/book-appointment",
            json={"phone_number": phone, "requested_datetime": first, "full_name": "Meera"},
            headers=TOOL_HEADERS,
        )
    ).json()
    assert booked["success"] is True
    assert booked["data"]["status"] == "booked"
    original_id = booked["data"]["appointment_id"]

    rescheduled = (
        await client.post(
            "/api/v1/agent-tools/reschedule-appointment",
            json={"phone_number": phone, "new_appointment_datetime": second},
            headers=TOOL_HEADERS,
        )
    ).json()
    assert rescheduled["success"] is True
    assert rescheduled["data"]["status"] == "rescheduled"
    new_id = rescheduled["data"]["appointment_id"]
    assert new_id != original_id

    original = await Appointment.get(original_id)
    new = await Appointment.get(new_id)
    assert original.status == "cancelled"
    assert new.original_appointment_id == original_id
    assert new.booking_source == "inbound_call"

    cancelled = (
        await client.post(
            "/api/v1/agent-tools/cancel-appointment",
            json={"phone_number": phone, "reason": "feeling better"},
            headers=TOOL_HEADERS,
        )
    ).json()
    assert cancelled["success"] is True
    assert cancelled["data"]["appointment_id"] == new_id
    assert cancelled["data"]["status"] == "cancelled"
    assert (await Appointment.get(new_id)).notes == "Cancelled: feeling better"


@pytest.mark.asyncio
async def test_agent_reschedule_to_the_same_time_is_refused_and_changes_nothing(
    client: AsyncClient, business_config
):
    phone = "+919800000011"
    slot = _next_weekday_at(0, 9).isoformat()
    booked = (
        await client.post(
            "/api/v1/agent-tools/book-appointment",
            json={"phone_number": phone, "requested_datetime": slot},
            headers=TOOL_HEADERS,
        )
    ).json()

    response = (
        await client.post(
            "/api/v1/agent-tools/reschedule-appointment",
            json={"phone_number": phone, "new_appointment_datetime": slot},
            headers=TOOL_HEADERS,
        )
    ).json()
    assert response["success"] is False
    assert response["message"] == "Slot is already booked"

    original = await Appointment.get(booked["data"]["appointment_id"])
    assert original.status == "booked"
    assert await Appointment.find(Appointment.person_id == original.person_id).count() == 1


@pytest.mark.asyncio
async def test_agent_cancel_of_already_cancelled_id_still_reports_success(client: AsyncClient, business_config):
    phone = "+919800000012"
    booked = (
        await client.post(
            "/api/v1/agent-tools/book-appointment",
            json={"phone_number": phone, "requested_datetime": _next_weekday_at(0, 9).isoformat()},
            headers=TOOL_HEADERS,
        )
    ).json()
    appointment_id = booked["data"]["appointment_id"]
    await client.post(
        "/api/v1/agent-tools/cancel-appointment", json={"phone_number": phone}, headers=TOOL_HEADERS
    )

    again = (
        await client.post(
            "/api/v1/agent-tools/cancel-appointment",
            json={"phone_number": phone, "appointment_id": appointment_id},
            headers=TOOL_HEADERS,
        )
    ).json()
    assert again["success"] is True
    assert again["data"]["appointment_id"] == appointment_id
    assert again["data"]["status"] == "cancelled"


# --- Notification lifecycles ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_whatsapp_lifecycle_book_then_reschedule(business_config):
    person = await _person("+919800000020", "Kiran")
    original = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )

    send = AsyncMock(side_effect=[WhatsAppSendResult(message_id="wamid.1"), WhatsAppSendResult(message_id="wamid.2")])
    with patch(SEND, send):
        await push_pending_whatsapp_notifications_once()
        new = await appointment_service.reschedule_existing(
            appointment_id=str(original.id), new_appointment_datetime=_next_weekday_at(0, 10)
        )
        await push_pending_whatsapp_notifications_once()
        await push_pending_whatsapp_notifications_once()  # nothing left — must not send again

    assert [c.kwargs["template_name"] for c in send.call_args_list] == [
        "appointment_booked_v1",
        "appointment_rescheduled_v1",
    ]
    assert send.call_args_list[0].kwargs["to"] == "919800000020"
    assert (await Appointment.get(original.id)).whatsapp_message_id == "wamid.1"
    refreshed_new = await Appointment.get(new.id)
    assert refreshed_new.whatsapp_message_id == "wamid.2"
    assert refreshed_new.whatsapp_notification_status == "sent"


@pytest.mark.asyncio
async def test_google_calendar_lifecycle_book_reschedule_cancel(business_config):
    person = await _person("+919800000021", "Dev")
    original = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )

    create = AsyncMock(side_effect=lambda event: GoogleCalendarEvent(id=f"evt-{event.id}"))
    delete = AsyncMock(return_value=None)
    with patch(CREATE_EVENT, create), patch(DELETE_EVENT, delete):
        await push_pending_google_calendar_appointments_once()
        new = await appointment_service.reschedule_existing(
            appointment_id=str(original.id), new_appointment_datetime=_next_weekday_at(0, 10)
        )
        await push_pending_google_calendar_appointments_once()
        await appointment_service.cancel(str(new.id), reason="no longer needed")
        await push_pending_google_calendar_appointments_once()
        await push_pending_google_calendar_appointments_once()  # nothing left

    assert [c.args[0].id for c in create.call_args_list] == [str(original.id), str(new.id)]
    assert [c.args[0] for c in delete.call_args_list] == [f"evt-{original.id}", f"evt-{new.id}"]
    for appointment_id in (original.id, new.id):
        refreshed = await Appointment.get(appointment_id)
        assert refreshed.status == "cancelled"
        assert refreshed.google_calendar_sync_status == "synced"


# --- Admin: cancel a scheduled call / reschedule an appointment ------------------------------


@pytest.mark.asyncio
async def test_admin_cancels_pending_scheduled_call_and_it_is_never_placed(
    client: AsyncClient, auth_headers: dict, admin, edesy_agent, business_config
):
    from app.models.audit_log import AuditLog

    person = await _person("+919800000030")
    schedule = await _due_schedule(person)

    response = await client.post(f"/api/v1/call-schedules/{schedule.id}/cancel", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(schedule.id)
    assert body["status"] == "cancelled"
    assert body["person_full_name"] is None or isinstance(body["person_full_name"], str)

    log = await AuditLog.find_one(AuditLog.entity_id == str(schedule.id), AuditLog.action == "cancel")
    assert log.actor_id == str(admin.id)
    assert log.actor_type == "admin"
    assert log.before["status"] == "pending"
    assert log.after["status"] == "cancelled"

    place_call = AsyncMock(return_value=EdesyPlaceCallResponse(call_id="sid-never"))
    with patch(PLACE_CALL, place_call):
        await dispatch_due_calls_once()
    place_call.assert_not_awaited()
    assert (await CallSchedule.get(schedule.id)).status == "cancelled"


@pytest.mark.asyncio
async def test_admin_cancel_of_unknown_scheduled_call_is_404(client: AsyncClient, auth_headers: dict):
    response = await client.post("/api/v1/call-schedules/6a0000000000000000000000/cancel", headers=auth_headers)
    assert response.status_code == 404
    assert response.json() == {"detail": "Call schedule not found"}


@pytest.mark.asyncio
async def test_admin_reschedule_flow(client: AsyncClient, auth_headers: dict, business_config):
    person = await _person("+919800000031")
    original = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )

    response = await client.post(
        f"/api/v1/appointments/{original.id}/reschedule",
        json={"new_appointment_datetime": _next_weekday_at(0, 10).isoformat(), "notes": "moved by admin"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rescheduled"
    assert body["original_appointment_id"] == str(original.id)
    assert body["notes"] == "moved by admin"
    assert (await Appointment.get(original.id)).status == "cancelled"

    active = await Appointment.find(
        Appointment.person_id == str(person.id), {"status": {"$in": ["booked", "rescheduled"]}}
    ).to_list()
    assert [str(a.id) for a in active] == [body["id"]]


@pytest.mark.asyncio
async def test_admin_reschedule_of_cancelled_appointment_is_refused(
    client: AsyncClient, auth_headers: dict, business_config
):
    person = await _person("+919800000032")
    original = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )
    await appointment_service.cancel(str(original.id))

    response = await client.post(
        f"/api/v1/appointments/{original.id}/reschedule",
        json={"new_appointment_datetime": _next_weekday_at(0, 10).isoformat()},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "No active appointment found to reschedule"}
    assert await Appointment.find(Appointment.person_id == str(person.id)).count() == 1
