"""Student-owned notice follows and persistent in-app notifications."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from finalsay.auth import require_role
from finalsay.db import get_db
from finalsay.models import (
    CategoryFollow,
    Institution,
    Notice,
    NoticeFollow,
    StudentNotification,
    User,
)
from finalsay.schemas import (
    CategoryFollowCreate,
    CategoryFollowOut,
    FollowedNoticeOut,
    StudentFollowsOut,
    StudentNotificationOut,
    StudentNotificationPage,
)
from finalsay.services.alerts import category_key

router = APIRouter(
    prefix="/api/alerts",
    tags=["student alerts"],
    dependencies=[Depends(require_role("student"))],
)


def _student_institution(user: User) -> int:
    if user.institution_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Choose your college before creating notice alerts.",
        )
    return user.institution_id


def _owned_notification(db: Session, notification_id: int, user: User) -> StudentNotification:
    notification = db.scalar(
        select(StudentNotification).where(
            StudentNotification.id == notification_id,
            StudentNotification.user_id == user.id,
            StudentNotification.dismissed_at.is_(None),
        )
    )
    if notification is None:
        raise HTTPException(status_code=404, detail="notification not found")
    return notification


@router.get("/unread-count")
def unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> dict[str, int]:
    count = db.scalar(
        select(func.count()).select_from(StudentNotification).where(
            StudentNotification.user_id == current_user.id,
            StudentNotification.read_at.is_(None),
            StudentNotification.dismissed_at.is_(None),
        )
    ) or 0
    return {"unread_count": count}


@router.get("/follows", response_model=StudentFollowsOut)
def list_follows(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> StudentFollowsOut:
    followed_notices = db.scalars(
        select(NoticeFollow)
        .join(Notice, Notice.id == NoticeFollow.notice_id)
        .where(
            NoticeFollow.user_id == current_user.id,
            Notice.kind == "official",
        )
        .order_by(NoticeFollow.created_at.desc())
    ).all()
    items: list[FollowedNoticeOut] = []
    for follow in followed_notices:
        notice = follow.notice
        if notice.source_key:
            latest = db.scalar(
                select(Notice).where(
                    Notice.kind == "official",
                    Notice.institution_id == notice.institution_id,
                    Notice.source_key == notice.source_key,
                    Notice.is_current.is_(True),
                    Notice.source_state == "active",
                    Notice.admin_status == "active",
                )
            )
            if latest is not None:
                notice = latest
        items.append(FollowedNoticeOut(notice=notice, followed_at=follow.created_at))
    categories = db.scalars(
        select(CategoryFollow)
        .where(CategoryFollow.user_id == current_user.id)
        .order_by(CategoryFollow.created_at.desc())
    ).all()
    return StudentFollowsOut(notices=items, categories=categories)


@router.post("/notices/{notice_id}/follow", status_code=status.HTTP_201_CREATED)
def follow_notice(
    notice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> dict[str, bool]:
    institution_id = _student_institution(current_user)
    notice = db.get(Notice, notice_id)
    if (
        notice is None
        or notice.kind != "official"
        or notice.institution_id != institution_id
        or not notice.is_current
        or notice.source_state != "active"
        or notice.admin_status != "active"
    ):
        raise HTTPException(status_code=404, detail="current official notice not found")
    existing = db.scalar(
        select(NoticeFollow).where(
            NoticeFollow.user_id == current_user.id,
            NoticeFollow.notice_id == notice.id,
        )
    )
    if existing is None:
        db.add(NoticeFollow(user_id=current_user.id, notice_id=notice.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(NoticeFollow).where(
                    NoticeFollow.user_id == current_user.id,
                    NoticeFollow.notice_id == notice.id,
                )
            )
            if existing is None:
                raise
    return {"followed": True}


@router.delete("/notices/{notice_id}/follow")
def unfollow_notice(
    notice_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> dict[str, bool]:
    follow = db.scalar(
        select(NoticeFollow).join(Notice, Notice.id == NoticeFollow.notice_id).where(
            NoticeFollow.user_id == current_user.id,
            NoticeFollow.notice_id == notice_id,
            Notice.institution_id == current_user.institution_id,
        )
    )
    if follow is not None:
        db.delete(follow)
        db.commit()
    return {"followed": False}


@router.post("/categories", response_model=CategoryFollowOut, status_code=status.HTTP_201_CREATED)
def follow_category(
    payload: CategoryFollowCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> CategoryFollow:
    institution_id = _student_institution(current_user)
    institution = db.get(Institution, institution_id)
    if institution is None or not institution.active:
        raise HTTPException(status_code=409, detail="Your selected college is unavailable.")
    label = " ".join(payload.category_label.split())
    key = category_key(label)
    if not key:
        raise HTTPException(status_code=422, detail="Enter a topic with letters or numbers.")
    existing = db.scalar(
        select(CategoryFollow).where(
            CategoryFollow.user_id == current_user.id,
            CategoryFollow.institution_id == institution_id,
            CategoryFollow.category_key == key,
        )
    )
    if existing is not None:
        return existing
    subscription_count = db.scalar(
        select(func.count()).select_from(CategoryFollow).where(
            CategoryFollow.user_id == current_user.id,
            CategoryFollow.institution_id == institution_id,
        )
    ) or 0
    if subscription_count >= 50:
        raise HTTPException(status_code=409, detail="You can follow up to 50 topics for this college.")
    follow = CategoryFollow(
        user_id=current_user.id,
        institution_id=institution_id,
        category_key=key,
        category_label=label,
    )
    db.add(follow)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(CategoryFollow).where(
                CategoryFollow.user_id == current_user.id,
                CategoryFollow.institution_id == institution_id,
                CategoryFollow.category_key == key,
            )
        )
        if existing is None:
            raise
        return existing
    db.refresh(follow)
    return follow


@router.delete("/categories/{follow_id}")
def unfollow_category(
    follow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> dict[str, bool]:
    follow = db.scalar(
        select(CategoryFollow).where(
            CategoryFollow.id == follow_id,
            CategoryFollow.user_id == current_user.id,
        )
    )
    if follow is not None:
        db.delete(follow)
        db.commit()
    return {"followed": False}


@router.get("", response_model=StudentNotificationPage)
def list_notifications(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    unread_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> StudentNotificationPage:
    query = select(StudentNotification).where(
        StudentNotification.user_id == current_user.id,
        StudentNotification.dismissed_at.is_(None),
    )
    if unread_only:
        query = query.where(StudentNotification.read_at.is_(None))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(
        query.order_by(StudentNotification.created_at.desc(), StudentNotification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    unread = db.scalar(
        select(func.count()).select_from(StudentNotification).where(
            StudentNotification.user_id == current_user.id,
            StudentNotification.read_at.is_(None),
            StudentNotification.dismissed_at.is_(None),
        )
    ) or 0
    items = []
    for row in rows:
        notice = db.get(Notice, row.notice_id) if row.notice_id else None
        previous = db.get(Notice, row.previous_notice_id) if row.previous_notice_id else None
        items.append(StudentNotificationOut(
            id=row.id,
            kind=row.kind,
            title=row.title,
            message=row.message,
            notice_id=row.notice_id,
            previous_notice_id=row.previous_notice_id,
            notice_title=notice.title if notice else None,
            previous_notice_title=previous.title if previous else None,
            source_url=notice.source_url if notice else None,
            created_at=row.created_at,
            read_at=row.read_at,
            old_value=row.old_value,
            new_value=row.new_value,
        ))
    return StudentNotificationPage(
        items=items,
        total=total,
        unread_count=unread,
        page=page,
        page_size=page_size,
    )


@router.patch("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> dict[str, int]:
    result = db.execute(
        update(StudentNotification)
        .where(
            StudentNotification.user_id == current_user.id,
            StudentNotification.read_at.is_(None),
            StudentNotification.dismissed_at.is_(None),
        )
        .values(read_at=datetime.now(timezone.utc))
    )
    db.commit()
    return {"updated": result.rowcount or 0}


@router.patch("/{notification_id}/read")
def mark_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> dict[str, bool]:
    notification = _owned_notification(db, notification_id, current_user)
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        db.commit()
    return {"read": True}


@router.delete("/{notification_id}")
def dismiss_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("student")),
) -> dict[str, bool]:
    notification = _owned_notification(db, notification_id, current_user)
    notification.dismissed_at = datetime.now(timezone.utc)
    db.commit()
    return {"dismissed": True}
