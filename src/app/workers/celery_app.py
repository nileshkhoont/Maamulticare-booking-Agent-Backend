from celery import Celery
from celery.signals import setup_logging

from app.core.config import settings
from app.core.logging import configure_logging


@setup_logging.connect
def _configure_celery_logging(**_kwargs) -> None:
    # Connecting to this signal tells Celery to skip its own logging.config setup entirely and
    # defer to ours instead — the documented way to keep worker/beat output in the same
    # structlog format as the API process instead of Celery's separate log format.
    configure_logging()


celery_app = Celery(
    "ai_calling_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.workers.tasks.outbound_call_task",
        "app.workers.tasks.missed_call_retry_task",
        "app.workers.tasks.recording_backfill_task",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# Registers the Beat schedule on this same app object, so both `celery ... worker` and
# `celery ... beat` can be pointed at `app.workers.celery_app` alone.
from app.workers import scheduler  # noqa: F401,E402
