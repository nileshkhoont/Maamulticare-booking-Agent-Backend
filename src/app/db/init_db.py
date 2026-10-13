"""Applies MongoDB $jsonSchema validators (schema doc §9) as a database-level safeguard on top
of the Pydantic/Beanie validation the API already does. Safe to run repeatedly — uses collMod on
an existing collection, createCollection on a new one.
"""

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.logging import get_logger

logger = get_logger(__name__)

APPOINTMENTS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["person_id", "appointment_datetime", "status", "booking_source", "created_at"],
        "properties": {
            "person_id": {"bsonType": "string"},
            "appointment_datetime": {"bsonType": "date"},
            "status": {"enum": ["booked", "rescheduled", "cancelled", "completed", "no_show"]},
            "booking_source": {"enum": ["inbound_call", "admin_scheduled_call"]},
        },
    }
}

CALL_SCHEDULES_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["person_id", "scheduled_at", "call_purpose", "requested_by", "status", "created_at"],
        "properties": {
            "person_id": {"bsonType": "string"},
            "scheduled_at": {"bsonType": "date"},
            "call_purpose": {"enum": ["admin_scheduled", "person_requested_callback"]},
            "requested_by": {"enum": ["admin", "system", "person"]},
            "status": {"enum": ["pending", "in_progress", "completed", "missed", "cancelled"]},
        },
    }
}

CALLS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["person_id", "call_type", "direction", "call_status", "created_at"],
        "properties": {
            "person_id": {"bsonType": "string"},
            "call_type": {"enum": ["inbound", "outbound_admin_scheduled"]},
            "direction": {"enum": ["inbound", "outbound"]},
            "call_status": {"enum": ["answered", "missed", "failed", "busy", "no_answer"]},
            "outcome": {
                "enum": [
                    "appointment_booked",
                    "appointment_rescheduled",
                    "callback_requested",
                    "no_action_taken",
                    None,
                ]
            },
        },
    }
}

VALIDATORS = {
    "appointments": APPOINTMENTS_VALIDATOR,
    "call_schedules": CALL_SCHEDULES_VALIDATOR,
    "calls": CALLS_VALIDATOR,
}


async def apply_schema_validators(db: AsyncIOMotorDatabase) -> None:
    existing = set(await db.list_collection_names())

    for collection_name, validator in VALIDATORS.items():
        if collection_name in existing:
            await db.command(
                "collMod",
                collection_name,
                validator=validator,
                validationLevel="strict",
                validationAction="error",
            )
        else:
            await db.create_collection(
                collection_name,
                validator=validator,
                validationLevel="strict",
                validationAction="error",
            )
        logger.info("schema_validator_applied", collection=collection_name)
