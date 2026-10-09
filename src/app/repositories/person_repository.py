import re

from beanie import PydanticObjectId

from app.models.person import Person
from app.schemas.common import PageParams
from app.utils.validators import normalize_phone_number, strip_unresolved_placeholder


class PersonRepository:
    async def get_by_id(self, person_id: str) -> Person | None:
        if not PydanticObjectId.is_valid(person_id):
            return None
        person = await Person.get(person_id)
        if person is None or person.is_deleted:
            return None
        return person

    async def get_many_by_ids(self, person_ids: set[str]) -> dict[str, Person]:
        """Batch lookup for list endpoints (calls/appointments/call-schedules) that need to show
        the person's name/phone next to each row without an N+1 query per row. Silently skips any
        id that isn't a well-formed ObjectId — a bad id on one row (e.g. a corrupt legacy record)
        must not take down the whole list.
        """
        object_ids = [PydanticObjectId(pid) for pid in person_ids if PydanticObjectId.is_valid(pid)]
        if not object_ids:
            return {}
        persons = await Person.find({"_id": {"$in": object_ids}}).to_list()
        return {str(p.id): p for p in persons}

    async def get_by_phone(self, phone_number: str) -> Person | None:
        """Normalizes before the exact-match lookup — Edesy has been observed sending the same
        real caller's number both as "+917600181441" and, on a different call, as "7600181441"
        (see normalize_phone_number's docstring), and this is an exact Person.phone_number
        comparison, so an unnormalized query would silently miss the existing record.
        """
        return await Person.find_one(
            Person.phone_number == normalize_phone_number(phone_number),
            Person.is_deleted == False,  # noqa: E712
        )

    async def get_by_email(self, email: str) -> Person | None:
        """Fallback person-resolution for a direct-Calendly booking's inbound webhook, when the
        invitee has no usable phone (text_reminder_number) to resolve via get_or_create_by_phone —
        e.g. the doctor's invite link didn't ask for one. Exact, case-insensitive match; deliberately
        NOT used for Calendly webhook *correlation* (that's always by invitee/event URI, never
        email — see calendly_sync_service.py — since many patients share the placeholder email).
        """
        return await Person.find_one(
            {"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}},
            Person.is_deleted == False,  # noqa: E712
        )

    async def apply_name_if_given(self, person: Person, full_name: str | None) -> Person:
        """Update-if-different name logic, factored out so any path that already has a resolved
        Person (not just get_or_create_by_phone below) — e.g. agent_tools.py's
        _resolve_person_for_call, which finds the Person via a verified call_schedule rather than
        by phone number — can still apply a caller-stated name the same safe way.

        A name is only ever *replaced* by another real name. Anything still shaped like an
        unsubstituted `{{token}}` is treated as "no name given" (see utils/validators.py):
        on 2026-09-22 this overwrote a real caller's name with the literal "{{full_name}}",
        and because agent-tool writes don't go through the admin PATCH endpoint there was no
        audit-log entry to trace it by.
        """
        full_name = strip_unresolved_placeholder(full_name)
        if full_name and person.full_name != full_name:
            person.full_name = full_name
            await person.save()
        return person

    async def get_or_create_by_phone(self, phone_number: str, full_name: str | None = None) -> Person:
        """Used both by the inbound call.started webhook handler (we don't know the caller's
        name yet, so a placeholder is used until the agent's identify_person tool call updates
        it) and by that identify_person tool itself.
        """
        phone_number = normalize_phone_number(phone_number)
        existing = await self.get_by_phone(phone_number)
        if existing:
            return await self.apply_name_if_given(existing, full_name)

        person = Person(
            full_name=strip_unresolved_placeholder(full_name) or phone_number, phone_number=phone_number
        )
        await person.insert()
        return person

    @staticmethod
    def _name_or_phone_filter(query: str) -> dict:
        """Case-insensitive substring match on full_name OR phone_number — deliberately plain
        $regex, not $text: $text does whole-word/stemmed matching only ("Harsh" matches, "Har"
        does not), which made the admin search bars across Calls/Appointments/Schedule feel dead
        until a full name was typed (2026-10-01). $regex with no anchors matches live, on every
        keystroke, anywhere in the name — same substring behavior phone_number already had.
        """
        escaped = re.escape(query)
        return {
            "$or": [
                {"full_name": {"$regex": escaped, "$options": "i"}},
                {"phone_number": {"$regex": escaped}},
            ]
        }

    async def search(self, query: str | None, page: PageParams) -> tuple[list[Person], int]:
        filter_query = Person.find(Person.is_deleted == False)  # noqa: E712
        if query:
            filter_query = Person.find(
                Person.is_deleted == False,  # noqa: E712
                self._name_or_phone_filter(query),
            )

        total = await filter_query.count()
        items = (
            await filter_query.sort(-Person.created_at)
            .skip(page.skip)
            .limit(page.page_size)
            .to_list()
        )
        return items, total

    async def find_ids_matching(self, query: str) -> list[str]:
        """Same regex match as search() above, but returns every matching id unpaginated — used
        by the calls/appointments/call-schedules list endpoints to search "by person" (they only
        store person_id, not a denormalized name/phone) by first resolving the query to a set of
        person ids and then filtering their own collection with {"person_id": {"$in": ...}}.
        """
        persons = await Person.find(
            Person.is_deleted == False,  # noqa: E712
            self._name_or_phone_filter(query),
        ).to_list()
        return [str(p.id) for p in persons]


person_repository = PersonRepository()
