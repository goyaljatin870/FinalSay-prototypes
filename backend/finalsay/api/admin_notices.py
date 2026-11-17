"""Admin review actions for automatically collected official notices."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from finalsay.auth import require_role
from finalsay.db import get_db
from finalsay.models import Notice, User
from finalsay.schemas import (
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
    notice.admin_status = payload.status
    notice.admin_review_note = payload.note.strip() if payload.note else None
    notice.admin_reviewed_at = datetime.now(timezone.utc)
    notice.admin_reviewed_by = current_user.id
    db.commit()
    db.refresh(notice)
    return notice
