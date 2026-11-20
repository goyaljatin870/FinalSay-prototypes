"""Admin review actions for automatically collected official notices."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from finalsay.auth import require_role
from finalsay.db import get_db
from finalsay.models import Notice, User
from finalsay.models import Institution
from finalsay.services import alerts, ingestion, provenance
from finalsay.schemas import (
    AdminOfficialNoticeRevision,
    AdminOfficialNoticeWrite,
    AdminNoticeOut,
    AdminNoticePage,
    AdminNoticeReviewUpdate,
)

router = APIRouter(
    prefix="/api/admin/notices",
    tags=["admin notice review"],
    dependencies=[Depends(require_role("admin"))],
)


@router.get("", response_model=AdminNoticePage)
def list_admin_notices(
    institution_id: int | None = Query(default=None, ge=1),
    status: str | None = Query(default=None, pattern="^(active|flagged|archived|rejected)$"),
    q: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> AdminNoticePage:
    query = select(Notice).where(Notice.kind == "official")
    if institution_id is not None:
        query = query.where(Notice.institution_id == institution_id)
    if status is not None:
        query = query.where(Notice.admin_status == status)
    if q and q.strip():
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{needle}%"
        query = query.where(
            or_(
                Notice.title.ilike(pattern, escape="\\"),
                Notice.issuer.ilike(pattern, escape="\\"),
                Notice.redacted_text.ilike(pattern, escape="\\"),
            )
        )
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    notices = db.scalars(
        query.order_by(Notice.created_at.desc(), Notice.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return AdminNoticePage(
        items=[AdminNoticeOut.model_validate(notice) for notice in notices],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=AdminNoticeOut, status_code=201)
def create_admin_notice(
    payload: AdminOfficialNoticeWrite,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_role("admin")),
) -> Notice:
    institution = db.get(Institution, payload.institution_id)
    if institution is None or not institution.active or institution.scrape_type == "fixture":
        raise HTTPException(status_code=404, detail="active supported institution not found")
    notice = ingestion.publish_official(
        db,
        text=payload.text,
        institution=institution,
        source_url=payload.source_url,
        source_key=f"admin:{institution.slug}:{uuid4().hex}",
        title=payload.title,
        published_date=payload.published_date,
    )
    provenance.build_daily_merkle(db, notice.created_at.strftime("%Y-%m-%d"))
    db.commit()
    db.refresh(notice)
    return notice


@router.put("/{notice_id}/content", response_model=AdminNoticeOut)
def revise_admin_notice(
    notice_id: int,
    payload: AdminOfficialNoticeRevision,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_role("admin")),
) -> Notice:
    notice = db.scalar(
        select(Notice)
        .options(joinedload(Notice.institution))
        .where(
            Notice.id == notice_id,
            Notice.kind == "official",
            Notice.is_current.is_(True),
        )
    )
    if notice is None:
        raise HTTPException(status_code=404, detail="current official notice not found")
    try:
        revised = ingestion.revise_official(
            db,
            notice=notice,
            text=payload.text,
            title=payload.title,
            source_url=payload.source_url,
            published_date=payload.published_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    provenance.build_daily_merkle(db, revised.created_at.strftime("%Y-%m-%d"))
    db.commit()
    db.refresh(revised)
    return revised


@router.patch("/{notice_id}/review", response_model=AdminNoticeOut)
def review_notice(
    notice_id: int,
    payload: AdminNoticeReviewUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
) -> Notice:
    notice = db.get(Notice, notice_id)
    if notice is None or notice.kind != "official":
        raise HTTPException(status_code=404, detail="official notice not found")
    previous_status = notice.admin_status
    notice.admin_status = payload.status
    notice.admin_review_note = payload.note.strip() if payload.note else None
    notice.admin_reviewed_at = datetime.now(timezone.utc)
    notice.admin_reviewed_by = current_user.id
    alerts.notify_review_status_change(db, notice, previous_status, payload.status)
    db.commit()
    db.refresh(notice)
    return notice
