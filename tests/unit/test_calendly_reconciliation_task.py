"""Covers the reconciliation sweep's inbound catch-up — specifically the client-side event_type
filter, since Calendly's GET /scheduled_events has no server-side filter for it (confirmed against
Calendly's own OpenAPI spec) and this account has more than one Event Type.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.integrations.calendly.schemas import (
    CalendlyInviteeResource,
    CalendlyScheduledEventResource,
    CalendlyScheduledEventResponse,
    CalendlyUserResource,
    CalendlyUserResponse,
)
from app.models.appointment import Appointment
from app.workers.tasks.calendly_reconciliation_task import _inbound_catch_up


@pytest.mark.asyncio
async def test_inbound_catch_up_ignores_events_from_other_event_types(business_config):
    """Two scheduled events come back from Calendly: one under this integration's configured
    Event Type, one under a completely different Event Type (e.g. the hospital's unrelated "OPD
    Consultation" type). Only the matching one should be processed — the other's invitee must
    never be turned into a local Appointment.
    """
    matching_event = CalendlyScheduledEventResource(
        uri="https://api.calendly.com/scheduled_events/evt-match",
        event_type=settings.calendly_event_type_uri,
        start_time="2026-11-10T05:30:00.000000Z",
        end_time="2026-11-10T05:50:00.000000Z",
        status="active",
    )
    other_event = CalendlyScheduledEventResource(
        uri="https://api.calendly.com/scheduled_events/evt-other",
        event_type="https://api.calendly.com/event_types/OPD000000000AAAA",
        start_time="2026-11-10T06:00:00.000000Z",
        end_time="2026-11-10T06:15:00.000000Z",
        status="active",
    )

    mock_get_current_user = AsyncMock(
        return_value=CalendlyUserResponse(
            resource=CalendlyUserResource(
                uri="https://api.calendly.com/users/test-user",
                current_organization="https://api.calendly.com/organizations/test-org",
            )
        )
    )
    mock_list_events = AsyncMock(side_effect=[[matching_event, other_event], []])  # active, then canceled
    mock_get_invitees = AsyncMock(
        return_value=[
            CalendlyInviteeResource(
                uri="https://api.calendly.com/scheduled_events/evt-match/invitees/inv-match",
                event="https://api.calendly.com/scheduled_events/evt-match",
                name="Matching Patient",
                email="matching@example.com",
                text_reminder_number="+919825000099",
                status="active",
            )
        ]
    )
    # _handle_invitee_created also checks this event's own event_type (the same filter applied
    # per-delivery that webhooks get — see test_calendly_webhooks.py's analogous test) before
    # doing anything else, so this must carry the matching Event Type URI too, not just a bare
    # start_time/end_time.
    mock_get_scheduled_event = AsyncMock(
        return_value=CalendlyScheduledEventResponse(
            resource=CalendlyScheduledEventResource(
                uri="https://api.calendly.com/scheduled_events/evt-match",
                event_type=settings.calendly_event_type_uri,
                start_time="2026-11-10T05:30:00.000000Z",
                end_time="2026-11-10T05:50:00.000000Z",
            )
        )
    )

    with (
        patch("app.workers.tasks.calendly_reconciliation_task.calendly_client.get_current_user", mock_get_current_user),
        patch("app.workers.tasks.calendly_reconciliation_task.calendly_client.list_scheduled_events", mock_list_events),
        patch("app.workers.tasks.calendly_reconciliation_task.calendly_client.get_event_invitees", mock_get_invitees),
        patch("app.services.calendly_sync_service.calendly_client.get_scheduled_event", mock_get_scheduled_event),
    ):
        processed = await _inbound_catch_up()

    assert processed == 1
    mock_get_invitees.assert_called_once_with("evt-match")

    created = await Appointment.find(Appointment.calendly_invitee_uri != None).to_list()  # noqa: E711
    assert len(created) == 1
    assert created[0].calendly_invitee_uri == "https://api.calendly.com/scheduled_events/evt-match/invitees/inv-match"
