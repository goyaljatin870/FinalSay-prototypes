"""Persistent student alert creation and topic matching for official notices.

Alerts are written in the same SQLAlchemy transaction as the notice upsert, so
scraper, issuer, and admin publishing paths cannot commit a version without its
corresponding alert event. Delivery is in-app; this module does not send email
or push notifications.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from dateutil import parser as date_parser

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from finalsay.models import (
    CategoryFollow,
    Notice,
    NoticeFollow,
    StudentNotification,
)
from finalsay.services.extraction import _DATE_RE

_CANCELLED = re.compile(
    r"\b(?:withdrawn|cancelled|canceled|stands\s+cancelled|stands\s+canceled)\b",
    re.IGNORECASE,
)
_SUPERSEDES = re.compile(
    r"\b(?:supersed(?:e|es|ed|ing)|replac(?:e|es|ed|ing)\s+by|"
    r"in\s+supersession\s+of|cancell?ation\s+of|withdraw(?:n|s|ing)|"
    r"revised\s+(?:schedule|notice|date)|extension\s+of)\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_GENERIC_CATEGORY_WORDS = {"notice", "notices", "department", "dept", "and", "of", "the"}
_CATEGORY_ALIASES = {
    "examination": {"examination", "examinations", "exam", "exams", "timetable", "date sheet"},
    "academic": {"academic", "academics", "semester", "course", "curriculum", "class"},
    "fee": {"fee", "fees", "payment", "tuition", "dues"},
    "placement": {"placement", "placements", "career", "recruitment", "internship"},
    "admission": {"admission", "admissions", "application", "enrollment", "enrolment"},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(value: str | None) -> str:
    return " ".join(_TOKEN_RE.findall((value or "").casefold()))


def _notice_text(notice: Notice) -> str:
    return "\n".join(
        value for value in (
            notice.title,
            notice.issuer,
            notice.audience,
            notice.action,
            notice.deadline,
            notice.redacted_text,
        ) if value
    )


def category_key(label: str) -> str:
    tokens = [
        token for token in _TOKEN_RE.findall(label.casefold())
        if token not in _GENERIC_CATEGORY_WORDS
    ]
    for canonical, aliases in _CATEGORY_ALIASES.items():
        if len(tokens) == 1 and tokens[0] in {
            alias for alias in aliases if " " not in alias
        }:
            tokens = [canonical]
            break
    return "-".join(tokens)[:120]


def category_matches(label: str, notice: Notice) -> bool:
    """Conservative phrase/token match against stored title and notice text."""
    source_tokens = set(_TOKEN_RE.findall(_notice_text(notice).casefold()))
    category_tokens = set(_TOKEN_RE.findall(label.casefold())) - _GENERIC_CATEGORY_WORDS
    if not category_tokens:
        return False
    canonical = " ".join(sorted(category_tokens))
    if canonical in _CATEGORY_ALIASES:
        return bool(source_tokens.intersection(_CATEGORY_ALIASES[canonical]))
    return category_tokens.issubset(source_tokens)


def _change_values(previous: Notice, current: Notice) -> tuple[str | None, str | None, str | None]:
    """Return a reliably named changed field and values, or no specific claim."""
    for label, old_value, new_value in (
        ("deadline", previous.deadline, current.deadline),
        ("notice date", previous.notice_date, current.notice_date),
        ("audience", previous.audience, current.audience),
        ("action", previous.action, current.action),
    ):
        if old_value and new_value and _normalize(old_value) != _normalize(new_value):
            return label, old_value.strip(), new_value.strip()

    currency = re.compile(r"(?:₹|\bINR\b|\bRs\.?\s*)\s*[\d,]+(?:\.\d{1,2})?", re.IGNORECASE)
    old_amounts = currency.findall(_notice_text(previous))
    new_amounts = currency.findall(_notice_text(current))
    if old_amounts and new_amounts and old_amounts != new_amounts:
        return "fee amount", old_amounts[0].strip(), new_amounts[0].strip()
    return None, None, None


def _meaningful_change(previous: Notice, current: Notice) -> bool:
    """Ignore whitespace/punctuation-only differences while retaining content edits."""
    fields = ("title", "issuer", "notice_date", "deadline", "audience", "action", "redacted_text")
    old = _canonical_notice_text(" ".join(getattr(previous, field) or "" for field in fields))
    new = _canonical_notice_text(" ".join(getattr(current, field) or "" for field in fields))
    return old != new


def _canonical_notice_text(value: str) -> str:
    """Normalize layout and equivalent date spellings before alerting."""
    def canonical_date(match: re.Match[str]) -> str:
        raw = match.group(0)
        has_year = bool(re.search(r"\b(?:19|20)\d{2}\b|[./-]\d{2,4}\b", raw))
        try:
            parsed = date_parser.parse(raw, dayfirst=True, default=datetime(2000, 1, 1))
        except (ValueError, OverflowError):
            return raw.casefold()
        year = parsed.year if has_year else "unspecified"
        return f"date{parsed.month:02d}{parsed.day:02d}{year}"

    dates_normalized = _DATE_RE.sub(canonical_date, value.casefold())
    dates_normalized = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", dates_normalized)
    return "".join(_TOKEN_RE.findall(dates_normalized))


def _insert_notification(
    db: Session,
    *,
    user_id: int,
    notice_id: int | None,
    previous_notice_id: int | None,
    kind: str,
    title: str,
    message: str,
    dedupe_key: str,
    old_value: str | None = None,
    new_value: str | None = None,
) -> None:
    values = {
        "user_id": user_id,
        "notice_id": notice_id,
        "previous_notice_id": previous_notice_id,
        "kind": kind,
        "title": title[:240],
        "message": message[:2_000],
        "old_value": old_value,
        "new_value": new_value,
        "dedupe_key": dedupe_key[:240],
        "created_at": _now(),
    }
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        stmt = sqlite_insert(StudentNotification).values(**values).on_conflict_do_nothing(
            index_elements=["user_id", "dedupe_key"]
        )
        db.execute(stmt)
    elif dialect == "postgresql":
        stmt = pg_insert(StudentNotification).values(**values).on_conflict_do_nothing(
            index_elements=["user_id", "dedupe_key"]
        )
        db.execute(stmt)
    else:
        existing = db.scalar(select(StudentNotification.id).where(
            StudentNotification.user_id == user_id,
            StudentNotification.dedupe_key == values["dedupe_key"],
        ))
        if existing is None:
            db.add(StudentNotification(**values))


def _followed_notices(db: Session, current: Notice, previous: Notice | None = None) -> list[NoticeFollow]:
    if previous is None:
        return []
    if current.source_key:
        query = (
            select(NoticeFollow)
            .join(Notice, Notice.id == NoticeFollow.notice_id)
            .where(
                Notice.institution_id == current.institution_id,
                Notice.source_key == current.source_key,
            )
        )
    else:
        query = select(NoticeFollow).where(NoticeFollow.notice_id == previous.id)
    return list(db.scalars(query).all())


def _followed_subscribers_for_related_notice(db: Session, current: Notice) -> list[tuple[NoticeFollow, Notice]]:
    if not current.institution_id or not _SUPERSEDES.search(_notice_text(current)):
        return []
    current_terms = set(_TOKEN_RE.findall(_notice_text(current).casefold()))
    results: list[tuple[NoticeFollow, Notice]] = []
    follows = db.execute(
        select(NoticeFollow, Notice)
        .join(Notice, Notice.id == NoticeFollow.notice_id)
        .where(
            Notice.institution_id == current.institution_id,
            Notice.kind == "official",
            Notice.id != current.id,
        )
    ).all()
    for follow, old in follows:
        old_title_terms = set(_TOKEN_RE.findall((old.title or "").casefold())) - _GENERIC_CATEGORY_WORDS
        if len(old_title_terms) < 2:
            continue
        overlap = len(old_title_terms & current_terms) / len(old_title_terms)
        if overlap >= 0.5:
            results.append((follow, old))
    return results


def notify_created_notice(db: Session, notice: Notice) -> None:
    """Notify matching topic subscribers and explicit cross-URL supersessions."""
    if notice.kind != "official" or not notice.is_current:
        return
    category_subscriptions = db.scalars(
        select(CategoryFollow).where(CategoryFollow.institution_id == notice.institution_id)
    ).all()
    for subscription in category_subscriptions:
        if category_matches(subscription.category_label, notice):
            title = f"New {subscription.category_label} notice"
            _insert_notification(
                db,
                user_id=subscription.user_id,
                notice_id=notice.id,
                previous_notice_id=None,
                kind="category_new_notice",
                title=title,
                message=f"A new official notice matches your followed topic “{subscription.category_label}”. Open it to review the source.",
                dedupe_key=f"category:{subscription.id}:notice:{notice.id}",
            )

    for follow, old in _followed_subscribers_for_related_notice(db, notice):
        cancelled = bool(_CANCELLED.search(_notice_text(notice)))
        _insert_notification(
            db,
            user_id=follow.user_id,
            notice_id=notice.id,
            previous_notice_id=old.id,
            kind="notice_cancelled" if cancelled else "notice_superseded",
            title="A followed notice was cancelled" if cancelled else "A newer notice may replace one you follow",
            message=(
                "A newer official notice explicitly refers to cancellation or withdrawal of a related notice you follow. Review the new source."
                if cancelled else
                "A newer official notice explicitly refers to a related notice you follow. Review it to see whether it replaces or changes the earlier instructions."
            ),
            dedupe_key=f"related:{follow.id}:notice:{notice.id}",
        )


def notify_updated_notice(db: Session, previous: Notice, current: Notice) -> None:
    """Notify all follows across a same-source version lineage of a real edit."""
    if not _meaningful_change(previous, current):
        return
    follows = _followed_notices(db, current, previous)
    if not follows:
        return
    field_name, old_value, new_value = _change_values(previous, current)
    cancelled = bool(_CANCELLED.search(_notice_text(current)))
    if cancelled:
        kind = "notice_cancelled"
        title = "A followed notice was cancelled"
        message = "The latest version says this official notice has been withdrawn or cancelled. Open the source to confirm the instructions."
    elif field_name:
        kind = "notice_changed"
        title = f"A followed notice changed its {field_name}"
        message = f"The {field_name} changed from “{old_value}” to “{new_value}”. Review the latest official notice."
    else:
        kind = "notice_updated"
        title = "A followed notice has a new version"
        message = "The official notice has changed. Open the latest version to review the updated instructions."
    for follow in follows:
        _insert_notification(
            db,
            user_id=follow.user_id,
            notice_id=current.id,
            previous_notice_id=previous.id,
            kind=kind,
            title=title,
            message=message,
            dedupe_key=f"version:{follow.id}:notice:{current.id}",
            old_value=old_value,
            new_value=new_value,
        )


def notify_source_removed(db: Session, notice: Notice) -> None:
    """Tell followers a completed source snapshot no longer lists the notice."""
    follows = _followed_notices(db, notice, notice)
    for follow in follows:
        _insert_notification(
            db,
            user_id=follow.user_id,
            notice_id=notice.id,
            previous_notice_id=None,
            kind="source_removed",
            title="A followed notice disappeared from the source portal",
            message="The university portal no longer lists this notice. FinalSay cannot tell whether it was withdrawn; check the source or contact the office.",
            dedupe_key=f"removed:{follow.id}:notice:{notice.id}",
        )


def notify_review_status_change(db: Session, notice: Notice, old_status: str, new_status: str) -> None:
    """Notify subscribers when moderation hides a notice from the student feed."""
    if old_status == new_status or new_status not in {"archived", "rejected"}:
        return
    follows = _followed_notices(db, notice, notice)
    for follow in follows:
        _insert_notification(
            db,
            user_id=follow.user_id,
            notice_id=notice.id,
            previous_notice_id=None,
            kind="notice_review_status",
            title="A followed notice is no longer in the active feed",
            message=(
                "FinalSay’s administrator changed this notice’s review status. This is not an official withdrawal notice; open the source or contact your institution to confirm its current status."
            ),
            dedupe_key=f"review:{follow.id}:notice:{notice.id}:{new_status}",
        )
