"""Shared lock and interval scheduler for configured source polling."""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, local

if sys.platform == "win32":
    import msvcrt

    class fcntl:  # minimal Windows stand-in for the two calls used below
        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 8

        @staticmethod
        def flock(fd, op):
            os.lseek(fd, 0, 0)
            if op & fcntl.LOCK_UN:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError from exc
else:
    import fcntl

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from finalsay.db import SessionLocal, engine
from finalsay.models import ApplicationSetting, ScrapeRun, ScrapeScheduleState
from finalsay.services.document_extraction_backfill import (
    refresh_unknown_document_extractions,
)
from finalsay.services.ingestion import fetch_all

_SCRAPE_LOCK = Lock()
_DATABASE_LOCK = local()
_POSTGRES_ADVISORY_LOCK_KEY = 0x46494E414C534159  # stable ASCII key: "FINALSAY"
_SOURCE_RETRY_BASE_SECONDS = 300
_SOURCE_RETRY_MAX_SECONDS = 3_600
_TRANSIENT_SOURCE_ERROR_MARKERS = (
    "request failed:",
    "could not resolve configured source host",
    "dns lookup timed out",
    "timed out",
    "timeouterror",
    "connection reset",
    "connection refused",
    "temporary failure",
    "temporarily unavailable",
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
)


def _source_retry_delay(interval_seconds: int, failure_streak: int) -> int:
    """Return a bounded exponential delay after a failed scheduled cycle."""
    exponent = max(0, failure_streak - 1)
    retry_seconds = min(
        _SOURCE_RETRY_MAX_SECONDS,
        _SOURCE_RETRY_BASE_SECONDS * (2**exponent),
    )
    return min(interval_seconds, retry_seconds)


def _configured_interval_seconds(fallback: int) -> int:
    """Read the admin override without making scheduler availability depend on it."""
    db = SessionLocal()
    try:
        setting = db.get(ApplicationSetting, "scrape_interval_seconds")
        if setting is None:
            return fallback
        value = int(setting.value)
        return value if value > 0 else fallback
    except Exception:  # noqa: BLE001 - retain env/default cadence if the setting store is unavailable
        return fallback
    finally:
        db.close()


def _wait_until_next_poll(
    stop_event: Event,
    interval_seconds: int,
) -> bool:
    """Wait on persisted due time and reload interval changes at least once/minute."""
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            state = db.get(ScrapeScheduleState, 1)
            if state is None:
                state = ScrapeScheduleState(
                    id=1,
                    status="idle",
                    next_scheduled_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                db.add(state)
                db.commit()
            if state.status == "running" or state.next_scheduled_at is None:
                delay = 0
            else:
                due = state.next_scheduled_at
                if due.tzinfo is not None:
                    due = due.astimezone(timezone.utc).replace(tzinfo=None)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                delay = max(0, int((due - now).total_seconds()))
        except Exception:  # noqa: BLE001 - retry promptly if durable schedule is unavailable
            db.rollback()
            delay = 0
        finally:
            db.close()
        if delay <= 0:
            return False
        if stop_event.wait(min(delay, 60)):
            return True
    return True


def _has_transient_source_errors(summary: dict, extraction_summary: dict) -> bool:
    """Only accelerate retries for failures likely to clear without operator action.

    Expected partial results such as an in-progress historical backfill,
    allowlist-skipped attachments, or a consistently oversized PDF remain on
    the normal cadence. Network and upstream service failures retry sooner.
    """
    errors = [
        str(result.get("error") or "")
        for result in summary.get("institutions", [])
    ]
    errors.extend(str(error) for error in extraction_summary.get("errors", []))
    if summary.get("errors") and not errors:
        # An aggregate error without source details is safer to retry.
        return True
    return any(
        marker in error.casefold()
        for error in errors
        for marker in _TRANSIENT_SOURCE_ERROR_MARKERS
    )


def _persist_document_extraction_errors(
    db: Session, extraction_summary: dict, logger: logging.Logger
) -> None:
    for institution_result in extraction_summary.get("institutions", []):
        institution_errors = institution_result.get("errors", [])
        if not institution_errors:
            continue
        for error in institution_errors:
            logger.warning(
                "Scheduled document extraction failed for %s: %s",
                institution_result["slug"],
                error,
            )
        latest_run = db.scalar(
            select(ScrapeRun)
            .where(ScrapeRun.institution_id == institution_result["institution_id"])
            .order_by(ScrapeRun.id.desc())
            .limit(1)
        )
        if latest_run is None:
            continue
        detail = "; ".join(institution_errors)[:1500]
        prefix = f"document extraction errors: {detail}"
        latest_run.error = (
            f"{latest_run.error} | {prefix}" if latest_run.error else prefix
        )[:4000]
        if latest_run.status == "ok":
            latest_run.status = "partial"


def acquire_scrape_slot() -> bool:
    """Claim the poll slot locally and, on PostgreSQL, across API processes.

    PostgreSQL holds a session-level advisory lock on a dedicated pooled
    connection for the duration of a poll. SQLite uses a host-local file lock
    so an API worker and a separate CLI/manual poll cannot overlap.
    """
    if not _SCRAPE_LOCK.acquire(blocking=False):
        return False

    connection = None
    try:
        if engine.dialect.name == "postgresql":
            connection = engine.connect()
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": _POSTGRES_ADVISORY_LOCK_KEY},
            ).scalar_one()
            connection.commit()
            if not acquired:
                connection.close()
                _SCRAPE_LOCK.release()
                return False
            _DATABASE_LOCK.connection = connection
        elif engine.dialect.name == "sqlite":
            database_path = engine.url.database
            if database_path and database_path != ":memory:":
                absolute_path = os.path.abspath(database_path)
                lock_name = hashlib.sha256(absolute_path.encode()).hexdigest()
                lock_path = os.path.join(
                    tempfile.gettempdir(), f"finalsay-scrape-{lock_name}.lock"
                )
                file_descriptor = os.open(
                    lock_path, os.O_CREAT | os.O_RDWR, 0o600
                )
                try:
                    fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(file_descriptor)
                    _SCRAPE_LOCK.release()
                    return False
                _DATABASE_LOCK.file_descriptor = file_descriptor
        return True
    except Exception:
        try:
            if connection is not None:
                connection.close()
        finally:
            _SCRAPE_LOCK.release()
        raise


def release_scrape_slot() -> None:
    connection = getattr(_DATABASE_LOCK, "connection", None)
    file_descriptor = getattr(_DATABASE_LOCK, "file_descriptor", None)
    try:
        if connection is not None:
            try:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": _POSTGRES_ADVISORY_LOCK_KEY},
                ).scalar_one()
                connection.commit()
            finally:
                connection.close()
                del _DATABASE_LOCK.connection
        if file_descriptor is not None:
            try:
                fcntl.flock(file_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(file_descriptor)
                del _DATABASE_LOCK.file_descriptor
    finally:
        _SCRAPE_LOCK.release()


def scheduled_scrape_loop(stop_event: Event, interval_seconds: int, logger: logging.Logger) -> None:
    """Run a durable, restart-resumable poll job without overlapping another poll.

    A fresh installation gets an immediately due poll. Normal restarts retain
    the persisted next due time. If a process dies mid-poll, the persisted
    ``running`` state is retried as soon as this worker owns the shared lock.
    """
    while not stop_event.is_set():
        interval_seconds = _configured_interval_seconds(interval_seconds)
        if _wait_until_next_poll(stop_event, interval_seconds):
            break
        try:
            acquired = acquire_scrape_slot()
        except Exception:  # noqa: BLE001 - retry coordination outages next cycle
            logger.exception("Could not acquire the source-poll coordination lock")
            if stop_event.wait(min(interval_seconds, 60)):
                break
            continue
        if not acquired:
            logger.info("Skipping scheduled notice poll because another poll is active")
            if stop_event.wait(min(interval_seconds, 60)):
                break
            continue
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            state = db.get(ScrapeScheduleState, 1)
            if state is None:
                state = ScrapeScheduleState(id=1, status="idle", next_scheduled_at=now)
                db.add(state)
                db.flush()
            if state.status != "running" and state.next_scheduled_at is not None:
                due = state.next_scheduled_at
                if due.tzinfo is not None:
                    due = due.astimezone(timezone.utc).replace(tzinfo=None)
                if due > now:
                    # Admin may have changed the interval after the wait check.
                    db.rollback()
                    continue
            failure_streak = state.failure_streak or 0
            state.status = "running"
            state.last_attempted_at = now
            state.last_error = None
            db.commit()
            summary = fetch_all(
                db, include_fixtures=False, commit_each_source=True
            )
            extraction_refresh = refresh_unknown_document_extractions(
                db, per_institution=2
            )
            _persist_document_extraction_errors(db, extraction_refresh, logger)
            interval_seconds = _configured_interval_seconds(interval_seconds)
            if _has_transient_source_errors(summary, extraction_refresh):
                failure_streak += 1
                next_delay = _source_retry_delay(interval_seconds, failure_streak)
                logger.warning(
                    "Source poll had transient errors; retrying in %d seconds (failure streak %d)",
                    next_delay,
                    failure_streak,
                )
            else:
                failure_streak = 0
                next_delay = interval_seconds
            state = db.get(ScrapeScheduleState, 1)
            state.status = (
                "partial"
                if summary.get("errors") or extraction_refresh.get("errors")
                else "ok"
            )
            state.last_completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            state.failure_streak = failure_streak
            state.next_scheduled_at = state.last_completed_at + timedelta(seconds=next_delay)
            errors = [
                f"{item.get('name') or item.get('slug')}: {item['error']}"
                for item in summary.get("institutions", [])
                if item.get("error")
            ]
            errors.extend(str(error) for error in extraction_refresh.get("errors", []))
            state.last_error = " | ".join(errors)[:4_000] or None
            db.commit()
            logger.info(
                "Scheduled notice poll completed: created=%d modified=%d unchanged=%d errors=%d extractions=%d extraction_errors=%d",
                summary["created"], summary["modified"], summary["skipped"], summary["errors"],
                extraction_refresh["stored"], len(extraction_refresh["errors"]),
            )
        except Exception:  # noqa: BLE001 - scheduler must survive a failed cycle
            db.rollback()
            logger.exception("Scheduled notice poll failed")
            try:
                state = db.get(ScrapeScheduleState, 1)
                if state is not None:
                    state.failure_streak = (state.failure_streak or 0) + 1
                    delay = _source_retry_delay(interval_seconds, state.failure_streak)
                    state.status = "error"
                    state.last_completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    state.next_scheduled_at = state.last_completed_at + timedelta(seconds=delay)
                    state.last_error = "Scheduled notice poll failed; see backend logs for details."
                    db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()
            release_scrape_slot()
