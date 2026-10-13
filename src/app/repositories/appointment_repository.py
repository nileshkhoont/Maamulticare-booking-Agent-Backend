from datetime import UTC, datetime

from beanie import PydanticObjectId

from app.core.constants import AppointmentStatus
from app.models.appointment import Appointment
from app.schemas.common import PageParams

ACTIVE_STATUSES = [AppointmentStatus.booked, AppointmentStatus.rescheduled]


class AppointmentRepository:
    async def get_by_id(self, appointment_id: str) -> Appointment | None:
        if not PydanticObjectId.is_valid(appointment_id):
            return None
        appointment = await Appointment.get(appointment_id)
        if appointment is None or appointment.is_deleted:
            return None
        return appointment

    async def get_active_for_person(self, person_id: str) -> Appointment | None:
        """The person's current active (booked/rescheduled) appointment, if any — this is what
        inbound-call handling checks first (PDF §2.1: "Agent checks whether the person already
        has an appointment").
        """
        return (
            await Appointment.find(
                Appointment.person_id == person_id,
                Appointment.is_deleted == False,  # noqa: E712
                {"status": {"$in": [s.value for s in ACTIVE_STATUSES]}},
            )
            .sort(-Appointment.appointment_datetime)
            .first_or_none()
        )

    async def list_upcoming_for_person(self, person_id: str) -> list[Appointment]:
        """Every active (booked/rescheduled) appointment for this person that is still in the
        future, soonest first — never a past one. get_active_for_person above (kept as-is for
        the "no upcoming — was their last one expired?" check) sorts by furthest-future
        appointment_datetime, so with two+ upcoming appointments it silently returns whichever
        is farthest away rather than the one actually booked most recently, and with zero
        upcoming ones it happily returns a booked-but-past appointment as if it still counted.
        Both were found 2026-09-23 to make the agent misreport whether/which appointment a
        caller has. Anything that means "does this person currently have an appointment" or
        "which ones can they reschedule/cancel" must use this method, not get_active_for_person.
        """
        return (
            await Appointment.find(
                Appointment.person_id == person_id,
                Appointment.is_deleted == False,  # noqa: E712
                {"status": {"$in": [s.value for s in ACTIVE_STATUSES]}},
                Appointment.appointment_datetime > datetime.now(UTC),
            )
            .sort(+Appointment.appointment_datetime)
            .to_list()
        )

    async def exists_active_at(
        self, appointment_datetime: datetime, exclude_appointment_id: str | None = None
    ) -> bool:
        query: dict = {
            "appointment_datetime": appointment_datetime,
            "status": {"$in": [s.value for s in ACTIVE_STATUSES]},
            "is_deleted": False,
        }
        if exclude_appointment_id:
            query["_id"] = {"$ne": exclude_appointment_id}
        return await Appointment.find(query).count() > 0

    async def list_active_between(self, start: datetime, end: datetime) -> list[Appointment]:
        """All booked/rescheduled appointments in a UTC range — used by
        slot_service.list_available_slots to filter a day's generated slot grid against real
        bookings in one query instead of one exists_active_at query per candidate slot.
        """
        return await Appointment.find(
            Appointment.appointment_datetime >= start,
            Appointment.appointment_datetime < end,
            Appointment.is_deleted == False,  # noqa: E712
            {"status": {"$in": [s.value for s in ACTIVE_STATUSES]}},
        ).to_list()

    async def list_for_person(self, person_id: str) -> list[Appointment]:
        return (
            await Appointment.find(
                Appointment.person_id == person_id, Appointment.is_deleted == False  # noqa: E712
            )
            .sort(-Appointment.appointment_datetime)
            .to_list()
        )

    async def list_filtered(
        self,
        page: PageParams,
        status: AppointmentStatus | None = None,
        person_ids: list[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[Appointment], int]:
        conditions: list = [Appointment.is_deleted == False]  # noqa: E712
        if status:
            conditions.append(Appointment.status == status)
        if person_ids is not None:
            conditions.append({"person_id": {"$in": person_ids}})
        if date_from or date_to:
            range_query: dict = {}
            if date_from:
                range_query["$gte"] = date_from
            if date_to:
                range_query["$lte"] = date_to
            conditions.append({"appointment_datetime": range_query})

        query = Appointment.find(*conditions)
        total = await query.count()
        items = (
            await query.sort(-Appointment.appointment_datetime)
            .skip(page.skip)
            .limit(page.page_size)
            .to_list()
        )
        return items, total

    async def list_booked_for_scheduling(self, person_id: str | None = None) -> list[Appointment]:
        """Feeds the admin's schedule-call form's "pick from already booked appointments" list
        (PDF §3.1).
        """
        conditions: list = [
            Appointment.is_deleted == False,  # noqa: E712
            {"status": {"$in": [s.value for s in ACTIVE_STATUSES]}},
        ]
        if person_id:
            conditions.append(Appointment.person_id == person_id)
        return await Appointment.find(*conditions).sort(-Appointment.appointment_datetime).to_list()


appointment_repository = AppointmentRepository()
