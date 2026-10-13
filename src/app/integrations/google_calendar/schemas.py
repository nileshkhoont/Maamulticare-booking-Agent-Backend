"""Pydantic mirror of the subset of a Google Calendar v3 `Event` resource this integration
actually reads/writes. Field names match Google's own camelCase wire format exactly (dateTime,
timeZone) since these cross the wire to/from their API verbatim. Typed loosely (extra="allow")
since a real Event resource has many more fields this integration never touches.
"""

from pydantic import BaseModel


class GoogleCalendarEventDateTime(BaseModel):
    dateTime: str  # RFC3339, e.g. "2026-11-01T15:30:00+05:30"
    timeZone: str | None = None  # IANA tz, e.g. "Asia/Kolkata"


class GoogleCalendarEvent(BaseModel):
    id: str | None = None
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    start: GoogleCalendarEventDateTime | None = None
    end: GoogleCalendarEventDateTime | None = None
    status: str | None = None  # "confirmed" | "tentative" | "cancelled"

    model_config = {"extra": "allow"}
