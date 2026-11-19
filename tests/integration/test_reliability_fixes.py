"""Regression tests for the queue/appointment reliability fixes. Each test reproduces one real bug
and FAILED against the pre-fix code (see the branch history); test_current_flows_preserved.py is
the companion file proving the normal flows are unchanged.

Numbering matches the fix list agreed for this branch:
  #1  stuck-sweeps used updated_at (never bumped) instead of when work actually started
  #3  the same call/notification could be picked up twice; dispatch overwrote the webhook result
  #4  cancel didn't check status; cancellation WhatsApp was never sent; agent ids not owner-checked
  #5  reschedule rollback restored the wrong status
  #6b Google Calendar 409 on create looped forever
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from beanie.operators import Set
from httpx import AsyncClient

from app.core.config import settings
from app.integrations.edesy.schemas import EdesyPlaceCallResponse
from app.integrations.google_calendar.schemas import GoogleCalendarEvent
from app.integrations.whatsapp.schemas import WhatsAppSendResult
from app.models.appointment import Appointment
from app.models.call_schedule import CallSchedule
from app.models.person import Person
from app.services.appointment_service import appointment_service
from app.workers.tasks.google_calendar_push_task import push_pending_google_calendar_appointments_once
from app.workers.tasks.google_calendar_reconciliation_task import reconcile_google_calendar_once
from app.workers.tasks.missed_call_retry_task import sweep_stuck_schedules_once
from app.workers.tasks.outbound_call_task import dispatch_due_calls_once
from app.workers.tasks.whatsapp_push_task import push_pending_whatsapp_notifications_once
from app.workers.tasks.whatsapp_reconciliation_task import reconcile_whatsapp_notifications_once

TOOL_HEADERS = {"X-Tool-Secret": settings.agent_tool_secret}
PLACE_CALL = "app.workers.tasks.outbound_call_task.edesy_client.place_call"
SEND = "app.services.whatsapp_notification_service.whatsapp_client.send_template_message"
CREATE_EVENT = "app.services.google_calendar_sync_service.google_calendar_client.create_event"
DELETE_EVENT = "app.services.google_calendar_sync_service.google_calendar_client.delete_event"
LONG_AGO = timedelta(hours=2)


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


async def _schedule(person: Person, **overrides) -> CallSchedule:
    fields = dict(
        person_id=str(person.id),
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        call_purpose="admin_scheduled",
        requested_by="admin",
        status="pending",
    )
    fields.update(overrides)
    schedule = CallSchedule(**fields)
    await schedule.insert()
    return schedule


async def _old_appointment(person: Person, hour: int, **overrides) -> Appointment:
    """An appointment created hours ago — the case where updated_at-based stuck checks misfire."""
    long_ago = datetime.now(UTC) - LONG_AGO
    fields = dict(
        person_id=str(person.id),
        appointment_datetime=_next_weekday_at(0, hour),
        status="booked",
        booking_source="inbound_call",
        created_at=long_ago,
        updated_at=long_ago,
    )
    fields.update(overrides)
    appointment = Appointment(**fields)
    await appointment.insert()
    return appointment


async def _set_schedule_status(schedule_id, status: str) -> None:
    await CallSchedule.find_one(CallSchedule.id == schedule_id).update(Set({CallSchedule.status: status}))


# --- #1 stuck-call sweep ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_1a_live_call_scheduled_long_ago_is_not_swept_as_missed(edesy_agent, business_config):
    person = await _person("+919811000001")
    long_ago = datetime.now(UTC) - LONG_AGO
    schedule = await _schedule(person, created_at=long_ago, updated_at=long_ago)

    with patch(PLACE_CALL, AsyncMock(return_value=EdesyPlaceCallResponse(call_id="sid-live"))):
        await dispatch_due_calls_once()

    assert await sweep_stuck_schedules_once() == 0
    assert (await CallSchedule.get(schedule.id)).status == "in_progress"


@pytest.mark.asyncio
async def test_1a_call_dispatched_long_ago_with_no_webhook_is_still_swept(business_config):
    person = await _person("+919811000002")
    schedule = await _schedule(person, status="in_progress", dispatched_at=datetime.now(UTC) - LONG_AGO)

    assert await sweep_stuck_schedules_once() == 1
    assert (await CallSchedule.get(schedule.id)).status == "missed"


@pytest.mark.asyncio
async def test_1c_sweep_never_overwrites_a_result_the_webhook_already_saved(business_config):
    person = await _person("+919811000003")
    long_ago = datetime.now(UTC) - LONG_AGO
    schedule = await _schedule(
        person, status="in_progress", created_at=long_ago, updated_at=long_ago, dispatched_at=long_ago
    )
    stale_snapshot = await CallSchedule.get(schedule.id)
    await _set_schedule_status(schedule.id, "completed")  # webhook lands after the sweep's read

    with patch(
        "app.workers.tasks.missed_call_retry_task.call_schedule_repository.list_stuck_in_progress",
        AsyncMock(return_value=[stale_snapshot]),
    ):
        await sweep_stuck_schedules_once()

    assert (await CallSchedule.get(schedule.id)).status == "completed"


# --- #1 Google Calendar / WhatsApp reconciliation ------------------------------------------


@pytest.mark.asyncio
async def test_1b_calendar_reconcile_never_resets_a_push_that_is_in_flight(business_config):
    person = await _person("+919811000010")
    appointment = await _old_appointment(person, 9, google_calendar_sync_status="pending")
    created = []

    async def slow_create(event):
        created.append(event.id)
        if len(created) == 1:  # while this create is in flight: reconcile runs, then the next push tick
            await reconcile_google_calendar_once()
            await push_pending_google_calendar_appointments_once()
        return GoogleCalendarEvent(id=event.id)

    with patch(CREATE_EVENT, AsyncMock(side_effect=slow_create)):
        await push_pending_google_calendar_appointments_once()

    assert created == [str(appointment.id)]
    assert (await Appointment.get(appointment.id)).google_calendar_sync_status == "synced"


@pytest.mark.asyncio
async def test_1b_calendar_row_syncing_for_too_long_is_reset(business_config):
    person = await _person("+919811000011")
    appointment = await _old_appointment(
        person,
        9,
        google_calendar_sync_status="syncing",
        google_calendar_sync_started_at=datetime.now(UTC) - LONG_AGO,
    )
    assert await reconcile_google_calendar_once() == 1
    assert (await Appointment.get(appointment.id)).google_calendar_sync_status == "pending"


@pytest.mark.asyncio
async def test_1b_whatsapp_reconcile_never_resets_a_send_that_is_in_flight(business_config):
    person = await _person("+919811000012")
    appointment = await _old_appointment(
        person, 9, whatsapp_notification_type="booked", whatsapp_notification_status="pending"
    )
    sent = []

    async def slow_send(**kwargs):
        sent.append(kwargs["template_name"])
        if len(sent) == 1:
            await reconcile_whatsapp_notifications_once()
            await push_pending_whatsapp_notifications_once()
        return WhatsAppSendResult(message_id=f"wamid.{len(sent)}")

    with patch(SEND, AsyncMock(side_effect=slow_send)):
        await push_pending_whatsapp_notifications_once()

    assert sent == ["appointment_booked_v1"]
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_notification_status == "sent"
    assert refreshed.whatsapp_notification_attempt_count == 1


@pytest.mark.asyncio
async def test_1b_whatsapp_row_sending_for_too_long_is_reset(business_config):
    person = await _person("+919811000013")
    appointment = await _old_appointment(
        person,
        9,
        whatsapp_notification_type="booked",
        whatsapp_notification_status="sending",
        whatsapp_send_started_at=datetime.now(UTC) - LONG_AGO,
    )
    assert await reconcile_whatsapp_notifications_once() == 1
    assert (await Appointment.get(appointment.id)).whatsapp_notification_status == "pending"


# --- #3 nothing picked up twice ------------------------------------------------------------


@pytest.mark.asyncio
async def test_3a_same_due_call_is_never_placed_twice(edesy_agent, business_config):
    person = await _person("+919811000020")
    schedule = await _schedule(person)
    stale_snapshot = await CallSchedule.get(schedule.id)  # what an overlapping tick would have read

    place_call = AsyncMock(return_value=EdesyPlaceCallResponse(call_id="sid-once"))
    with patch(PLACE_CALL, place_call):
        await dispatch_due_calls_once()
        with patch(
            "app.workers.tasks.outbound_call_task.call_schedule_repository.list_due",
            AsyncMock(return_value=[stale_snapshot]),
        ):
            await dispatch_due_calls_once()

    assert place_call.await_count == 1


@pytest.mark.asyncio
async def test_3b_dispatch_does_not_overwrite_a_fast_webhook_result(edesy_agent, business_config):
    """DEFENSIVE, not a reproduced real-world bug: the real call.ended webhook finds its schedule
    by edesy_call_id, which is only stored by this very write, so in practice nothing else can
    complete the row before it. This pins that dispatch's post-call write touches edesy_call_id
    only and can never overwrite a status some other writer set in the meantime.
    """
    person = await _person("+919811000021")
    schedule = await _schedule(person)

    async def place_call_then_webhook_lands(**kwargs):
        await _set_schedule_status(schedule.id, "completed")
        return EdesyPlaceCallResponse(call_id="sid-fast")

    with patch(PLACE_CALL, AsyncMock(side_effect=place_call_then_webhook_lands)):
        await dispatch_due_calls_once()

    refreshed = await CallSchedule.get(schedule.id)
    assert refreshed.status == "completed"
    assert refreshed.edesy_call_id == "sid-fast"


@pytest.mark.asyncio
async def test_3c_whatsapp_stale_pending_snapshot_is_not_sent_twice(business_config):
    person = await _person("+919811000022")
    appointment = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )
    stale_snapshot = await Appointment.get(appointment.id)

    send = AsyncMock(return_value=WhatsAppSendResult(message_id="wamid.once"))
    with patch(SEND, send):
        await push_pending_whatsapp_notifications_once()
        with patch(
            "app.workers.tasks.whatsapp_push_task.appointment_repository.list_pending_whatsapp_notifications",
            AsyncMock(return_value=[stale_snapshot]),
        ):
            await push_pending_whatsapp_notifications_once()

    assert send.await_count == 1


@pytest.mark.asyncio
async def test_3c_calendar_stale_pending_snapshot_is_not_created_twice(business_config):
    person = await _person("+919811000023")
    appointment = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )
    stale_snapshot = await Appointment.get(appointment.id)

    create = AsyncMock(return_value=GoogleCalendarEvent(id="evt-once"))
    with patch(CREATE_EVENT, create):
        await push_pending_google_calendar_appointments_once()
        with patch(
            "app.workers.tasks.google_calendar_push_task.appointment_repository.list_pending_google_calendar_sync",
            AsyncMock(return_value=[stale_snapshot]),
        ):
            await push_pending_google_calendar_appointments_once()

    assert create.await_count == 1


# --- #4 cancel ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_4b_cancelling_after_the_confirmation_was_sent_sends_the_cancellation(business_config):
    person = await _person("+919811000030")
    appointment = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )

    send = AsyncMock(side_effect=[WhatsAppSendResult(message_id="wamid.B"), WhatsAppSendResult(message_id="wamid.C")])
    with patch(SEND, send):
        await push_pending_whatsapp_notifications_once()
        await appointment_service.cancel(str(appointment.id))
        await push_pending_whatsapp_notifications_once()

    assert [c.kwargs["template_name"] for c in send.call_args_list] == [
        "appointment_booked_v1",
        "appointment_cancelled_v1",
    ]
    refreshed = await Appointment.get(appointment.id)
    assert refreshed.whatsapp_message_id == "wamid.C"
    assert refreshed.whatsapp_notification_status == "sent"


@pytest.mark.asyncio
async def test_4b_cancel_during_an_in_flight_confirmation_still_sends_the_cancellation(business_config):
    person = await _person("+919811000031")
    appointment = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )
    templates = []

    async def send(**kwargs):
        templates.append(kwargs["template_name"])
        if len(templates) == 1:  # admin cancels while the "booked" message is still being sent
            await appointment_service.cancel(str(appointment.id))
        return WhatsAppSendResult(message_id=f"wamid.{len(templates)}")

    with patch(SEND, AsyncMock(side_effect=send)):
        await push_pending_whatsapp_notifications_once()
        await push_pending_whatsapp_notifications_once()

    assert templates == ["appointment_booked_v1", "appointment_cancelled_v1"]


@pytest.mark.asyncio
async def test_cancel_during_an_in_flight_calendar_create_still_deletes_the_event(business_config):
    person = await _person("+919811000032")
    appointment = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )

    async def create(event):
        await appointment_service.cancel(str(appointment.id))  # cancelled while create is in flight
        return GoogleCalendarEvent(id="evt-ghost")

    delete = AsyncMock(return_value=None)
    with patch(CREATE_EVENT, AsyncMock(side_effect=create)), patch(DELETE_EVENT, delete):
        await push_pending_google_calendar_appointments_once()
        await push_pending_google_calendar_appointments_once()

    delete.assert_awaited_once_with("evt-ghost")
    assert (await Appointment.get(appointment.id)).google_calendar_sync_status == "synced"


@pytest.mark.asyncio
async def test_4a_admin_cannot_cancel_an_already_cancelled_appointment(
    client: AsyncClient, auth_headers: dict, business_config
):
    person = await _person("+919811000033")
    created = await client.post(
        "/api/v1/appointments",
        json={
            "person_id": str(person.id),
            "appointment_datetime": _next_weekday_at(0, 9).isoformat(),
            "booking_source": "admin_scheduled_call",
        },
        headers=auth_headers,
    )
    appointment_id = created.json()["id"]

    first = await client.post(
        f"/api/v1/appointments/{appointment_id}/cancel", json={"reason": "first"}, headers=auth_headers
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/appointments/{appointment_id}/cancel", json={"reason": "second"}, headers=auth_headers
    )
    assert second.status_code == 409
    assert (await Appointment.get(appointment_id)).notes == "Cancelled: first"


@pytest.mark.asyncio
async def test_4a_agent_repeat_cancel_reports_success_but_changes_nothing(client: AsyncClient, business_config):
    phone = "+919811000034"
    booked = (
        await client.post(
            "/api/v1/agent-tools/book-appointment",
            json={"phone_number": phone, "requested_datetime": _next_weekday_at(0, 9).isoformat()},
            headers=TOOL_HEADERS,
        )
    ).json()
    appointment_id = booked["data"]["appointment_id"]
    await client.post(
        "/api/v1/agent-tools/cancel-appointment",
        json={"phone_number": phone, "reason": "first"},
        headers=TOOL_HEADERS,
    )
    # The cancellation WhatsApp has since been delivered.
    await (await Appointment.get(appointment_id)).update(
        Set({Appointment.whatsapp_notification_status: "delivered", Appointment.whatsapp_message_id: "wamid.C"})
    )

    again = (
        await client.post(
            "/api/v1/agent-tools/cancel-appointment",
            json={"phone_number": phone, "appointment_id": appointment_id, "reason": "second"},
            headers=TOOL_HEADERS,
        )
    ).json()
    assert again["success"] is True
    assert again["data"]["status"] == "cancelled"

    refreshed = await Appointment.get(appointment_id)
    assert refreshed.notes == "Cancelled: first"
    assert refreshed.whatsapp_notification_status == "delivered"  # not re-queued


@pytest.mark.asyncio
async def test_4c_agent_cannot_cancel_or_reschedule_another_callers_appointment(
    client: AsyncClient, business_config
):
    owner_booking = (
        await client.post(
            "/api/v1/agent-tools/book-appointment",
            json={"phone_number": "+919811000035", "requested_datetime": _next_weekday_at(0, 9).isoformat()},
            headers=TOOL_HEADERS,
        )
    ).json()
    owners_id = owner_booking["data"]["appointment_id"]

    other_caller = "+919811000036"
    cancel = (
        await client.post(
            "/api/v1/agent-tools/cancel-appointment",
            json={"phone_number": other_caller, "appointment_id": owners_id},
            headers=TOOL_HEADERS,
        )
    ).json()
    reschedule = (
        await client.post(
            "/api/v1/agent-tools/reschedule-appointment",
            json={
                "phone_number": other_caller,
                "appointment_id": owners_id,
                "new_appointment_datetime": _next_weekday_at(0, 11).isoformat(),
            },
            headers=TOOL_HEADERS,
        )
    ).json()

    assert cancel["success"] is False
    assert reschedule["success"] is False
    owners = await Appointment.get(owners_id)
    assert owners.status == "booked"
    assert await Appointment.find(Appointment.person_id == owners.person_id).count() == 1


# --- #5 reschedule --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5a_failed_reschedule_leaves_the_original_exactly_as_it_was(business_config):
    from app.core.exceptions import SlotUnavailableError
    from app.schemas.appointment import SlotCheckResponse

    person = await _person("+919811000040")
    other = await _person("+919811000041")
    original = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )
    taken = _next_weekday_at(0, 10)
    await appointment_service.book_first_time(
        person_id=str(other.id), appointment_datetime=taken, booking_source="inbound_call"
    )
    before = await Appointment.get(original.id)

    # Both requests pass the availability check, then the unique index rejects the insert — the
    # same race as two truly concurrent bookings.
    with patch(
        "app.services.appointment_service.slot_service.check_availability",
        AsyncMock(return_value=SlotCheckResponse(available=True)),
    ):
        with pytest.raises(SlotUnavailableError):
            await appointment_service.reschedule_existing(
                appointment_id=str(original.id), new_appointment_datetime=taken
            )

    after = await Appointment.get(original.id)
    assert after.status == "booked"
    assert after.model_dump(exclude={"revision_id"}) == before.model_dump(exclude={"revision_id"})


# --- #6b Google Calendar 409 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_6b_calendar_409_on_create_adopts_the_existing_event(business_config):
    import httpx

    from app.integrations.google_calendar.client import GoogleCalendarClient

    def response(status: int, body: dict, method: str) -> httpx.Response:
        return httpx.Response(
            status_code=status, json=body, request=httpx.Request(method, "https://www.googleapis.com/x")
        )

    client = GoogleCalendarClient()
    request = AsyncMock(
        side_effect=[
            response(409, {"error": {"message": "The requested identifier already exists."}}, "POST"),
            response(200, {"id": "abc123", "status": "confirmed"}, "GET"),
        ]
    )
    with patch.object(client, "_headers", AsyncMock(return_value={})), patch("httpx.AsyncClient.request", request):
        result = await client.create_event(GoogleCalendarEvent(id="abc123", summary="x"))

    assert result.id == "abc123"
    assert [c.args[0] for c in request.call_args_list] == ["POST", "GET"]


# --- End-to-end races with REAL concurrency / the REAL webhook (no stale-snapshot shortcuts) ---


async def _post_call_ended(client: AsyncClient, call_sid: str, phone: str) -> None:
    import json

    payload = {
        "event": "call.ended",
        "timestamp": datetime.now(UTC).isoformat(),
        "call": {"callSid": call_sid, "direction": "outbound", "from": phone, "duration": 60},
        "outcome": {"status": "completed", "disposition": "APPOINTMENT_BOOKED", "endReason": "USER_REQUEST"},
        "transcript": [{"speaker": "agent", "text": "Hello!"}],
    }
    response = await client.post(
        "/api/v1/webhooks/edesy",
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.edesy_webhook_secret}"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_bug2_sweep_does_not_overwrite_the_real_webhooks_completed_result(client: AsyncClient, business_config):
    """The sweep reads a long-stuck in_progress row, then the REAL call.ended webhook completes it,
    then the sweep writes. The webhook's result must survive."""
    person = await _person("+919811000050")
    long_ago = datetime.now(UTC) - LONG_AGO
    schedule = await _schedule(
        person,
        status="in_progress",
        edesy_call_id="sid-late-webhook",
        created_at=long_ago,
        updated_at=long_ago,
        dispatched_at=long_ago,
    )
    stale_read_by_sweep = await CallSchedule.get(schedule.id)

    await _post_call_ended(client, "sid-late-webhook", person.phone_number)
    assert (await CallSchedule.get(schedule.id)).status == "completed"

    with patch(
        "app.workers.tasks.missed_call_retry_task.call_schedule_repository.list_stuck_in_progress",
        AsyncMock(return_value=[stale_read_by_sweep]),
    ):
        await sweep_stuck_schedules_once()

    assert (await CallSchedule.get(schedule.id)).status == "completed"


@pytest.mark.asyncio
async def test_bug3_two_overlapping_dispatch_runs_call_each_patient_once(edesy_agent, business_config):
    """Two dispatch runs genuinely in flight at the same time (Edesy slow to answer), two calls due."""
    import asyncio

    first = await _schedule(await _person("+919811000051"))
    second = await _schedule(await _person("+919811000052"))
    placed: list[str] = []

    async def slow_place_call(**kwargs):
        placed.append(kwargs["idempotency_key"])
        await asyncio.sleep(0.05)
        return EdesyPlaceCallResponse(call_id=f"sid-{len(placed)}")

    with patch(PLACE_CALL, AsyncMock(side_effect=slow_place_call)):
        await asyncio.gather(dispatch_due_calls_once(), dispatch_due_calls_once())

    assert sorted(placed) == sorted([str(first.id), str(second.id)])


@pytest.mark.asyncio
async def test_bug6_two_overlapping_whatsapp_runs_message_each_patient_once(business_config):
    import asyncio

    for phone, hour in (("+919811000053", 9), ("+919811000054", 10)):
        person = await _person(phone)
        await appointment_service.book_first_time(
            person_id=str(person.id), appointment_datetime=_next_weekday_at(0, hour), booking_source="inbound_call"
        )
    sent_to: list[str] = []

    async def slow_send(**kwargs):
        sent_to.append(kwargs["to"])
        await asyncio.sleep(0.05)
        return WhatsAppSendResult(message_id=f"wamid.{len(sent_to)}")

    with patch(SEND, AsyncMock(side_effect=slow_send)):
        await asyncio.gather(push_pending_whatsapp_notifications_once(), push_pending_whatsapp_notifications_once())

    assert sorted(sent_to) == ["919811000053", "919811000054"]


@pytest.mark.asyncio
async def test_bug6_two_overlapping_calendar_runs_create_each_event_once(business_config):
    import asyncio

    ids = []
    for phone, hour in (("+919811000055", 9), ("+919811000056", 10)):
        person = await _person(phone)
        appointment = await appointment_service.book_first_time(
            person_id=str(person.id), appointment_datetime=_next_weekday_at(0, hour), booking_source="inbound_call"
        )
        ids.append(str(appointment.id))
    created: list[str] = []

    async def slow_create(event):
        created.append(event.id)
        await asyncio.sleep(0.05)
        return GoogleCalendarEvent(id=event.id)

    with patch(CREATE_EVENT, AsyncMock(side_effect=slow_create)):
        await asyncio.gather(
            push_pending_google_calendar_appointments_once(), push_pending_google_calendar_appointments_once()
        )

    assert sorted(created) == sorted(ids)


@pytest.mark.asyncio
async def test_bug7_cancelling_a_rescheduled_appointment_sends_the_cancellation(business_config):
    person = await _person("+919811000057")
    original = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )
    templates: list[str] = []

    def send(**kwargs):
        templates.append(kwargs["template_name"])
        return WhatsAppSendResult(message_id=f"wamid.{len(templates)}")

    with patch(SEND, AsyncMock(side_effect=send)):
        await push_pending_whatsapp_notifications_once()
        new = await appointment_service.reschedule_existing(str(original.id), _next_weekday_at(0, 10))
        await push_pending_whatsapp_notifications_once()
        await appointment_service.cancel(str(new.id))
        await push_pending_whatsapp_notifications_once()

    assert templates == ["appointment_booked_v1", "appointment_rescheduled_v1", "appointment_cancelled_v1"]


@pytest.mark.asyncio
async def test_bug9_calendar_409_through_the_sync_service_ends_synced_and_deletable(business_config):
    """Full path: create answers 409 -> client adopts the existing event -> row synced with its id ->
    a later cancel deletes that event."""
    import httpx

    from app.services.google_calendar_sync_service import google_calendar_client

    person = await _person("+919811000058")
    appointment = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )

    def response(status: int, body: dict, method: str) -> httpx.Response:
        return httpx.Response(status_code=status, json=body, request=httpx.Request(method, "https://g/x"))

    request = AsyncMock(
        side_effect=[
            response(409, {"error": {"message": "The requested identifier already exists."}}, "POST"),
            response(200, {"id": str(appointment.id), "status": "confirmed"}, "GET"),
            response(204, {}, "DELETE"),
        ]
    )
    with patch.object(google_calendar_client, "_headers", AsyncMock(return_value={})), patch(
        "httpx.AsyncClient.request", request
    ):
        await push_pending_google_calendar_appointments_once()
        synced = await Appointment.get(appointment.id)
        assert synced.google_calendar_sync_status == "synced"
        assert synced.google_calendar_event_id == str(appointment.id)

        await appointment_service.cancel(str(appointment.id))
        await push_pending_google_calendar_appointments_once()

    assert [c.args[0] for c in request.call_args_list] == ["POST", "GET", "DELETE"]
    assert (await Appointment.get(appointment.id)).google_calendar_sync_status == "synced"


@pytest.mark.asyncio
async def test_bug9_calendar_409_for_a_deleted_event_is_reported_not_adopted(business_config):
    import httpx

    from app.core.exceptions import GoogleCalendarIntegrationError
    from app.integrations.google_calendar.client import GoogleCalendarClient

    def response(status: int, body: dict, method: str) -> httpx.Response:
        return httpx.Response(status_code=status, json=body, request=httpx.Request(method, "https://g/x"))

    client = GoogleCalendarClient()
    request = AsyncMock(
        side_effect=[
            response(409, {"error": {"message": "The requested identifier already exists."}}, "POST"),
            response(200, {"id": "abc123", "status": "cancelled"}, "GET"),
        ]
    )
    with patch.object(client, "_headers", AsyncMock(return_value={})), patch("httpx.AsyncClient.request", request):
        with pytest.raises(GoogleCalendarIntegrationError, match="cancelled"):
            await client.create_event(GoogleCalendarEvent(id="abc123", summary="x"))


@pytest.mark.asyncio
async def test_bug10_two_simultaneous_cancels_only_one_wins(business_config):
    import asyncio

    person = await _person("+919811000059")
    appointment = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )

    results = await asyncio.gather(
        appointment_service.cancel(str(appointment.id), reason="admin"),
        appointment_service.cancel(str(appointment.id), reason="agent"),
        return_exceptions=True,
    )

    succeeded = [r for r in results if isinstance(r, Appointment)]
    refused = [r for r in results if isinstance(r, Exception)]
    assert len(succeeded) == 1 and len(refused) == 1
    assert type(refused[0]).__name__ == "AppointmentNotActiveError"
    notes = (await Appointment.get(appointment.id)).notes
    assert notes in ("Cancelled: admin", "Cancelled: agent")


@pytest.mark.asyncio
async def test_bug10_admin_second_cancel_returns_a_clear_409_message(
    client: AsyncClient, auth_headers: dict, business_config
):
    person = await _person("+919811000060")
    appointment = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )
    await client.post(f"/api/v1/appointments/{appointment.id}/cancel", json={}, headers=auth_headers)
    second = await client.post(f"/api/v1/appointments/{appointment.id}/cancel", json={}, headers=auth_headers)

    assert second.status_code == 409
    assert second.json() == {"detail": "This appointment is already cancelled"}


# --- Admin cancel of a scheduled call that already started/finished --------------------------


@pytest.mark.asyncio
async def test_cancel_schedule_refused_once_the_call_is_being_placed(
    client: AsyncClient, auth_headers: dict, business_config
):
    person = await _person("+919811000070")
    schedule = await _schedule(
        person, status="in_progress", edesy_call_id="sid-ringing", dispatched_at=datetime.now(UTC)
    )

    response = await client.post(f"/api/v1/call-schedules/{schedule.id}/cancel", headers=auth_headers)

    assert response.status_code == 409
    assert response.json() == {"detail": "This call has already started and can no longer be cancelled"}
    refreshed = await CallSchedule.get(schedule.id)
    assert refreshed.status == "in_progress"
    assert refreshed.edesy_call_id == "sid-ringing"


@pytest.mark.asyncio
async def test_cancel_schedule_from_a_stale_page_does_not_rewrite_a_finished_call(
    client: AsyncClient, auth_headers: dict, business_config
):
    from app.models.audit_log import AuditLog

    person = await _person("+919811000071")
    schedule = await _schedule(person, status="completed", edesy_call_id="sid-done")

    response = await client.post(f"/api/v1/call-schedules/{schedule.id}/cancel", headers=auth_headers)

    assert response.status_code == 409
    assert response.json() == {"detail": "This call has already finished"}
    assert (await CallSchedule.get(schedule.id)).status == "completed"
    assert await AuditLog.find(AuditLog.entity_id == str(schedule.id)).count() == 0


@pytest.mark.asyncio
async def test_cancel_racing_with_dispatch_keeps_the_call_linked_as_outbound(
    client: AsyncClient, auth_headers: dict, edesy_agent, business_config
):
    """The admin's cancel reads the row while it's still pending; dispatch then claims it and places
    the call; then the cancel writes. The cancel must lose (the phone is already ringing), and must
    not erase the call id — otherwise the webhook can't link the call and records it as inbound."""
    from app.models.call import Call

    person = await _person("+919811000072")
    schedule = await _schedule(person)
    read_by_cancel_while_pending = await CallSchedule.get(schedule.id)

    with patch(PLACE_CALL, AsyncMock(return_value=EdesyPlaceCallResponse(call_id="sid-race"))):
        await dispatch_due_calls_once()

    with patch(
        "app.services.call_schedule_service.call_schedule_repository.get_by_id",
        AsyncMock(return_value=read_by_cancel_while_pending),
    ):
        response = await client.post(f"/api/v1/call-schedules/{schedule.id}/cancel", headers=auth_headers)
    assert response.status_code == 409

    refreshed = await CallSchedule.get(schedule.id)
    assert refreshed.status == "in_progress"
    assert refreshed.edesy_call_id == "sid-race"

    await _post_call_ended(client, "sid-race", person.phone_number)
    call = await Call.find_one(Call.edesy_call_id == "sid-race")
    assert call.call_type == "outbound_admin_scheduled"
    assert call.call_schedule_id == str(schedule.id)
    assert (await CallSchedule.get(schedule.id)).status == "completed"


# --- Two reschedules of the same appointment at the same moment -------------------------------


@pytest.mark.asyncio
async def test_two_simultaneous_reschedules_leave_exactly_one_appointment(business_config):
    import asyncio

    from app.core.exceptions import AppError

    person = await _person("+919811000073")
    original = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )

    results = await asyncio.gather(
        appointment_service.reschedule_existing(str(original.id), _next_weekday_at(0, 10)),  # admin
        appointment_service.reschedule_existing(str(original.id), _next_weekday_at(0, 11)),  # agent
        return_exceptions=True,
    )

    winners = [r for r in results if isinstance(r, Appointment)]
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == 1 and len(losers) == 1
    assert isinstance(losers[0], AppError)
    assert losers[0].message == "No active appointment found to reschedule"

    active = await Appointment.find(
        Appointment.person_id == str(person.id), {"status": {"$in": ["booked", "rescheduled"]}}
    ).to_list()
    assert [a.id for a in active] == [winners[0].id]

    templates: list[str] = []

    def send(**kwargs):
        templates.append(kwargs["template_name"])
        return WhatsAppSendResult(message_id=f"wamid.{len(templates)}")

    with patch(SEND, AsyncMock(side_effect=send)):
        await push_pending_whatsapp_notifications_once()
    # The original's own (unsent) "booked" confirmation is dropped by the reschedule, and only the
    # winning reschedule notifies the patient — never two "rescheduled" messages.
    assert templates == ["appointment_rescheduled_v1"]


@pytest.mark.asyncio
async def test_reschedule_refused_if_the_appointment_was_cancelled_meanwhile(business_config):
    person = await _person("+919811000074")
    original = await appointment_service.book_first_time(
        person_id=str(person.id), appointment_datetime=_next_weekday_at(0, 9), booking_source="inbound_call"
    )
    read_by_reschedule_while_active = await Appointment.get(original.id)
    await appointment_service.cancel(str(original.id))  # the patient cancels on a call meanwhile

    from app.core.exceptions import AppError

    with patch(
        "app.services.appointment_service.appointment_repository.get_by_id",
        AsyncMock(return_value=read_by_reschedule_while_active),
    ):
        with pytest.raises(AppError, match="No active appointment found to reschedule"):
            await appointment_service.reschedule_existing(str(original.id), _next_weekday_at(0, 10))

    active = await Appointment.find(
        Appointment.person_id == str(person.id), {"status": {"$in": ["booked", "rescheduled"]}}
    ).count()
    assert active == 0
