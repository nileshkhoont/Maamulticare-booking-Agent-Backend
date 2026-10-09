"""Dev-only fallback for the outbound call_schedules queue: runs the exact same dispatch and
stuck-schedule-sweep logic Celery Beat would trigger, but on a plain asyncio timer inside the API
process itself — no Redis, no separate worker/beat process. Enabled via
ENABLE_INPROCESS_SCHEDULER=true (see core/config.py); OFF by default since workers/celery_app.py +
Redis remains the documented, production-intended path (handles multiple API instances, survives
an API restart mid-dispatch, etc. — none of which this fallback does).
"""

import asyncio

from app.core.config import settings
from app.core.logging import get_logger
from app.workers.tasks.calendly_push_task import push_pending_calendly_appointments_once
from app.workers.tasks.calendly_reconciliation_task import reconcile_calendly_once
from app.workers.tasks.missed_call_retry_task import sweep_stuck_schedules_once
from app.workers.tasks.outbound_call_task import dispatch_due_calls_once
from app.workers.tasks.recording_backfill_task import backfill_missing_recordings_once

logger = get_logger(__name__)

_dispatch_task: asyncio.Task | None = None
_sweep_task: asyncio.Task | None = None
_recording_task: asyncio.Task | None = None
_calendly_push_task: asyncio.Task | None = None
_calendly_reconciliation_task: asyncio.Task | None = None

SWEEP_INTERVAL_SECONDS = 600  # matches workers/scheduler.py's Celery Beat schedule
RECORDING_BACKFILL_INTERVAL_SECONDS = 300  # matches workers/scheduler.py's Celery Beat schedule
CALENDLY_RECONCILIATION_INTERVAL_SECONDS = 600  # matches workers/scheduler.py's Celery Beat schedule


async def _dispatch_loop() -> None:
    while True:
        try:
            dispatched = await dispatch_due_calls_once()
            if dispatched:
                logger.info("inprocess_scheduler_dispatched", count=dispatched)
        except Exception:
            logger.exception("inprocess_scheduler_dispatch_error")
        await asyncio.sleep(settings.outbound_call_poll_interval_seconds)


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            swept = await sweep_stuck_schedules_once()
            if swept:
                logger.info("inprocess_scheduler_swept", count=swept)
        except Exception:
            logger.exception("inprocess_scheduler_sweep_error")


async def _recording_backfill_loop() -> None:
    while True:
        await asyncio.sleep(RECORDING_BACKFILL_INTERVAL_SECONDS)
        try:
            filled = await backfill_missing_recordings_once()
            if filled:
                logger.info("inprocess_scheduler_recordings_backfilled", count=filled)
        except Exception:
            logger.exception("inprocess_scheduler_recording_backfill_error")


async def _calendly_push_loop() -> None:
    while True:
        try:
            pushed = await push_pending_calendly_appointments_once()
            if pushed:
                logger.info("inprocess_scheduler_calendly_pushed", count=pushed)
        except Exception:
            logger.exception("inprocess_scheduler_calendly_push_error")
        await asyncio.sleep(settings.calendly_push_poll_interval_seconds)


async def _calendly_reconciliation_loop() -> None:
    while True:
        await asyncio.sleep(CALENDLY_RECONCILIATION_INTERVAL_SECONDS)
        try:
            processed = await reconcile_calendly_once()
            if processed:
                logger.info("inprocess_scheduler_calendly_reconciled", count=processed)
        except Exception:
            logger.exception("inprocess_scheduler_calendly_reconciliation_error")


def start() -> None:
    global _dispatch_task, _sweep_task, _recording_task, _calendly_push_task, _calendly_reconciliation_task
    if _dispatch_task is None:
        _dispatch_task = asyncio.create_task(_dispatch_loop())
    if _sweep_task is None:
        _sweep_task = asyncio.create_task(_sweep_loop())
    if _recording_task is None:
        _recording_task = asyncio.create_task(_recording_backfill_loop())
    if _calendly_push_task is None:
        _calendly_push_task = asyncio.create_task(_calendly_push_loop())
    if _calendly_reconciliation_task is None:
        _calendly_reconciliation_task = asyncio.create_task(_calendly_reconciliation_loop())
    logger.warning(
        "inprocess_scheduler_started",
        note="Dev fallback active — set up Celery+Redis (see backend/README.md) for production.",
        poll_interval_seconds=settings.outbound_call_poll_interval_seconds,
    )


def stop() -> None:
    global _dispatch_task, _sweep_task, _recording_task, _calendly_push_task, _calendly_reconciliation_task
    for task in (
        _dispatch_task,
        _sweep_task,
        _recording_task,
        _calendly_push_task,
        _calendly_reconciliation_task,
    ):
        if task:
            task.cancel()
    _dispatch_task = None
    _sweep_task = None
    _recording_task = None
    _calendly_push_task = None
    _calendly_reconciliation_task = None
