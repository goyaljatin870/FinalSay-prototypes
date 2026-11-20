from __future__ import annotations

from sqlalchemy import func, select

from finalsay.adapters.base import RawNotice
from finalsay.models import (
    CategoryFollow,
    Institution,
    IssuerMembership,
    Notice,
    NoticeFollow,
    StudentNotification,
    User,
)
from finalsay.services import ingestion


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _student(client, email: str, institution_id: int) -> str:
    response = client.post("/api/auth/register", json={
        "email": email,
        "password": "secret123",
        "institution_id": institution_id,
    })
    assert response.status_code == 201, response.text
    login = client.post("/api/auth/login", data={
        "username": email,
        "password": "secret123",
    })
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


def _institution(db_session) -> Institution:
    institution = Institution(
        name="Alerts University",
        slug="alerts-university",
        active=True,
        scrape_type="thapar_html",
    )
    db_session.add(institution)
    db_session.commit()
    return institution


def _published(db_session, institution: Institution, text: str, key: str = "https://university.example/notice.pdf", title: str | None = None) -> Notice:
    notice, _ = ingestion.upsert_official(
        db_session,
        RawNotice(
            text=text,
            source_url=key,
            institution_slug=institution.slug,
            external_id=key,
            metadata={"title": title or text.splitlines()[0][:500], "source_date": "12 Nov 2026"},
        ),
        institution,
    )
    db_session.commit()
    return notice


def test_follow_unfollow_is_persistent_and_duplicate_safe(client, db_session):
    institution = _institution(db_session)
    notice = _published(db_session, institution, "Deadline: 30 November 2026")
    token = _student(client, "follow-one@example.com", institution.id)

    first = client.post(f"/api/alerts/notices/{notice.id}/follow", headers=_headers(token))
    second = client.post(f"/api/alerts/notices/{notice.id}/follow", headers=_headers(token))
    assert first.status_code == 201
    assert second.status_code == 201
    assert db_session.scalar(select(func.count()).select_from(NoticeFollow)) == 1
    saved = client.get("/api/alerts/follows", headers=_headers(token))
    assert saved.status_code == 200
    assert saved.json()["notices"][0]["notice"]["id"] == notice.id
    relogin = client.post("/api/auth/login", data={"username": "follow-one@example.com", "password": "secret123"})
    persisted = client.get("/api/alerts/follows", headers=_headers(relogin.json()["access_token"]))
    assert persisted.json()["notices"][0]["notice"]["id"] == notice.id

    removed = client.delete(f"/api/alerts/notices/{notice.id}/follow", headers=_headers(token))
    assert removed.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(NoticeFollow)) == 0


def test_notice_update_creates_specific_alerts_for_every_follower_once(client, db_session):
    institution = _institution(db_session)
    original = _published(db_session, institution, "Hostel fee payment deadline: 30 November 2026")
    token_a = _student(client, "follow-a@example.com", institution.id)
    token_b = _student(client, "follow-b@example.com", institution.id)
    for token in (token_a, token_b):
        assert client.post(
            f"/api/alerts/notices/{original.id}/follow", headers=_headers(token)
        ).status_code == 201

    revised = _published(
        db_session,
        institution,
        "Hostel fee payment deadline: 25 November 2026",
    )
    assert revised.id != original.id
    assert revised.previous_version_id == original.id
    notifications = db_session.scalars(
        select(StudentNotification).order_by(StudentNotification.user_id)
    ).all()
    assert len(notifications) == 2
    assert all(row.kind == "notice_changed" for row in notifications)
    assert all(row.notice_id == revised.id and row.previous_notice_id == original.id for row in notifications)
    assert all(row.old_value and "30 November" in row.old_value for row in notifications)
    assert all(row.new_value and "25 November" in row.new_value for row in notifications)

    repeated = _published(
        db_session,
        institution,
        "Hostel fee payment deadline: 25 November 2026",
    )
    assert repeated.id == revised.id
    assert db_session.scalar(select(func.count()).select_from(StudentNotification)) == 2


def test_format_only_revision_does_not_generate_alert(client, db_session):
    institution = _institution(db_session)
    original = _published(db_session, institution, "Hostel fee deadline 30 November 2026")
    token = _student(client, "format-student@example.com", institution.id)
    client.post(f"/api/alerts/notices/{original.id}/follow", headers=_headers(token))

    reformatted = _published(db_session, institution, "  Hostel   fee deadline 30 November 2026  ")
    assert reformatted.previous_version_id == original.id
    assert db_session.scalar(select(func.count()).select_from(StudentNotification)) == 0


def test_equivalent_date_format_revision_does_not_generate_alert(client, db_session):
    institution = _institution(db_session)
    original = _published(db_session, institution, "Hostel fee deadline: 30 November 2026")
    token = _student(client, "date-format@example.com", institution.id)
    client.post(f"/api/alerts/notices/{original.id}/follow", headers=_headers(token))
    reformatted = _published(db_session, institution, "Hostel fee deadline: 30/11/2026")
    assert reformatted.previous_version_id == original.id
    assert db_session.scalar(select(func.count()).select_from(StudentNotification)) == 0


def test_category_subscription_and_new_notice_alert(client, db_session):
    institution = _institution(db_session)
    token = _student(client, "category-student@example.com", institution.id)
    added = client.post(
        "/api/alerts/categories",
        json={"category_label": "Examination Notices"},
        headers=_headers(token),
    )
    assert added.status_code == 201, added.text
    duplicate = client.post(
        "/api/alerts/categories",
        json={"category_label": "Exam"},
        headers=_headers(token),
    )
    assert duplicate.status_code == 201
    assert db_session.scalar(select(func.count()).select_from(CategoryFollow)) == 1

    _published(db_session, institution, "Hostel cafeteria payment schedule", key="https://university.example/hostel")
    assert db_session.scalar(select(func.count()).select_from(StudentNotification)) == 0
    _published(
        db_session,
        institution,
        "Revised examination timetable for the odd semester",
        key="https://university.example/examination",
    )
    alert = db_session.scalar(select(StudentNotification))
    assert alert is not None
    assert alert.kind == "category_new_notice"
    assert "Examination Notices" in alert.message
    category_follow_id = client.get("/api/alerts/follows", headers=_headers(token)).json()["categories"][0]["id"]
    assert client.delete(f"/api/alerts/categories/{category_follow_id}", headers=_headers(token)).status_code == 200
    assert client.get("/api/alerts/follows", headers=_headers(token)).json()["categories"] == []


def test_notification_read_dismiss_and_ownership(client, db_session):
    institution = _institution(db_session)
    _published(db_session, institution, "Exam timetable")
    token_a = _student(client, "owner-a@example.com", institution.id)
    token_b = _student(client, "owner-b@example.com", institution.id)
    client.post(
        "/api/alerts/categories",
        json={"category_label": "Examination"},
        headers=_headers(token_a),
    )
    _published(db_session, institution, "Revised examination timetable", key="https://university.example/exam-update")
    alert = db_session.scalar(select(StudentNotification))
    assert alert is not None

    assert client.get("/api/alerts/unread-count", headers=_headers(token_a)).json()["unread_count"] == 1
    forbidden = client.patch(f"/api/alerts/{alert.id}/read", headers=_headers(token_b))
    assert forbidden.status_code == 404
    marked = client.patch(f"/api/alerts/{alert.id}/read", headers=_headers(token_a))
    assert marked.status_code == 200
    assert client.get("/api/alerts/unread-count", headers=_headers(token_a)).json()["unread_count"] == 0
    dismissed = client.delete(f"/api/alerts/{alert.id}", headers=_headers(token_a))
    assert dismissed.status_code == 200
    assert client.get("/api/alerts", headers=_headers(token_a)).json()["total"] == 0


def test_source_snapshot_removal_alert_is_cautious_and_idempotent(db_session):
    from types import SimpleNamespace

    institution = _institution(db_session)
    notice = Notice(
        institution_id=institution.id,
        kind="official",
        title="Examination date sheet",
        source_url="https://university.example/date-sheet",
        source_key="https://university.example/date-sheet",
        extraction_quality="extracted",
        redacted_text="Examination date sheet",
    )
    db_session.add(notice)
    db_session.commit()
    user = User(email="removed@example.com", hashed_password="hash", role="student", institution_id=institution.id)
    db_session.add(user)
    db_session.flush()
    db_session.add(NoticeFollow(user_id=user.id, notice_id=notice.id))
    db_session.commit()

    adapter = SimpleNamespace(source_snapshot_complete=True, source_snapshot_keys={"https://university.example/another-notice"})
    ingestion._apply_source_snapshot(db_session, institution, adapter)
    db_session.commit()
    ingestion._apply_source_snapshot(db_session, institution, adapter)
    db_session.commit()
    alerts = db_session.scalars(select(StudentNotification)).all()
    assert len(alerts) == 1
    assert alerts[0].kind == "source_removed"
    assert "cannot tell whether it was withdrawn" in alerts[0].message


def test_scheduled_scraper_update_delivers_once_to_multiple_followers(client, db_session, monkeypatch):
    from types import SimpleNamespace

    from finalsay.adapters import live

    institution = _institution(db_session)
    institution.scrape_enabled = True
    db_session.commit()
    token_a = _student(client, "scraper-follow-a@example.com", institution.id)
    token_b = _student(client, "scraper-follow-b@example.com", institution.id)
    current_text = "Hostel retention fee payment deadline: 30 November 2026"
    raw = RawNotice(
        text=current_text,
        source_url="https://university.example/scraped-hostel-notice",
        institution_slug=institution.slug,
        external_id="hostel-retention-2026",
        metadata={"title": "Hostel retention fee payment", "extraction_quality": "extracted"},
    )

    class Adapter:
        slug = institution.slug
        name = institution.name
        base_url = "https://university.example"
        errors = []
        supports_backfill = False

        def fetch(self):
            return [raw]

    adapter = Adapter()
    monkeypatch.setattr(live, "create_live_adapter", lambda source: adapter)
    first = ingestion.fetch_all(db_session, source_slugs={institution.slug})
    assert first["created"] == 1
    db_session.commit()
    notice = db_session.scalar(select(Notice).where(Notice.kind == "official"))
    assert notice is not None
    for token in (token_a, token_b):
        assert client.post(f"/api/alerts/notices/{notice.id}/follow", headers=_headers(token)).status_code == 201

    raw.text = "Hostel retention fee payment deadline: 25 November 2026"
    second = ingestion.fetch_all(db_session, source_slugs={institution.slug})
    assert second["modified"] == 1
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(StudentNotification)) == 2

    third = ingestion.fetch_all(db_session, source_slugs={institution.slug})
    assert third["modified"] == 0
    assert third["skipped"] == 1
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(StudentNotification)) == 2


def test_admin_publish_and_revision_use_version_pipeline_and_alerts(client, db_session):
    institution = _institution(db_session)
    from finalsay.auth import hash_password
    admin = User(email="alerts-admin@example.com", hashed_password=hash_password("secret123"), role="admin")
    db_session.add(admin)
    db_session.commit()
    from finalsay.auth import create_access_token
    token = create_access_token(str(admin.id), "admin")
    headers = _headers(token)
    student_token = _student(client, "admin-revision-follow@example.com", institution.id)
    created = client.post(
        "/api/admin/notices",
        headers=headers,
        json={
            "institution_id": institution.id,
            "title": "Hostel retention fee deadline",
            "text": "Hostel retention fee payment deadline: 30 November 2026",
            "source_url": "https://university.example/admin-notice",
        },
    )
    assert created.status_code == 201, created.text
    notice_id = created.json()["id"]
    client.post(f"/api/alerts/notices/{notice_id}/follow", headers=_headers(student_token))
    revised = client.put(
        f"/api/admin/notices/{notice_id}/content",
        headers=headers,
        json={
            "title": "Hostel retention fee deadline revised",
            "text": "Hostel retention fee payment deadline: 25 November 2026",
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["previous_version_id"] == notice_id
    notification = client.get("/api/alerts", headers=_headers(student_token)).json()["items"][0]
    assert notification["kind"] == "notice_changed"
    archived = client.patch(
        f"/api/admin/notices/{revised.json()['id']}/review",
        headers=headers,
        json={"status": "archived", "note": "Source review complete"},
    )
    assert archived.status_code == 200, archived.text
    items = client.get("/api/alerts", headers=_headers(student_token)).json()["items"]
    status_alert = next(item for item in items if item["kind"] == "notice_review_status")
    assert "not an official withdrawal notice" in status_alert["message"]


def test_explicit_cross_url_supersession_and_withdrawal_alert(client, db_session):
    institution = _institution(db_session)
    original = _published(
        db_session,
        institution,
        "Hostel fee payment deadline: 30 November 2026",
        key="https://university.example/hostel-original",
        title="Hostel fee payment deadline",
    )
    token = _student(client, "superseded-follow@example.com", institution.id)
    client.post(f"/api/alerts/notices/{original.id}/follow", headers=_headers(token))

    _published(
        db_session,
        institution,
        "This notice supersedes the Hostel fee payment deadline notice. The deadline is 25 November 2026.",
        key="https://university.example/hostel-revision",
        title="Revised hostel fee payment schedule",
    )
    superseded = client.get("/api/alerts", headers=_headers(token)).json()["items"]
    assert len(superseded) == 1
    assert superseded[0]["kind"] == "notice_superseded"
    assert superseded[0]["previous_notice_title"] == original.title

    version = _published(
        db_session,
        institution,
        "This hostel fee payment notice is cancelled. Do not submit payment under this schedule.",
        key="https://university.example/hostel-original",
        title="Cancelled hostel fee payment deadline",
    )
    assert version.previous_version_id == original.id
    kinds = {item["kind"] for item in client.get("/api/alerts", headers=_headers(token)).json()["items"]}
    assert "notice_cancelled" in kinds


def test_issuer_publish_updates_are_delivered_through_existing_endpoint(client, db_session):
    from finalsay.auth import create_access_token, hash_password

    institution = _institution(db_session)
    issuer = User(email="notice-issuer@example.com", hashed_password=hash_password("secret123"), role="issuer")
    db_session.add(issuer)
    db_session.flush()
    db_session.add(IssuerMembership(user_id=issuer.id, institution_id=institution.id, active=True))
    db_session.commit()
    issuer_headers = _headers(create_access_token(str(issuer.id), "issuer"))
    student_token = _student(client, "issuer-follow@example.com", institution.id)
    first = client.post("/api/issuer/publish", headers=issuer_headers, json={
        "institution_id": institution.id,
        "source_url": "https://university.example/issuer-fees",
        "text": "Subject: Hostel fee payment schedule\nDeadline: 30 November 2026",
    })
    assert first.status_code == 201, first.text
    notice_id = first.json()["id"]
    client.post(f"/api/alerts/notices/{notice_id}/follow", headers=_headers(student_token))
    second = client.post("/api/issuer/publish", headers=issuer_headers, json={
        "institution_id": institution.id,
        "source_url": "https://university.example/issuer-fees",
        "text": "Subject: Hostel fee payment schedule\nDeadline: 25 November 2026",
    })
    assert second.status_code == 201, second.text
    alert = client.get("/api/alerts", headers=_headers(student_token)).json()["items"][0]
    assert alert["kind"] == "notice_changed"
    assert alert["previous_notice_id"] == notice_id


def test_mark_all_read_updates_unread_count(client, db_session):
    institution = _institution(db_session)
    token = _student(client, "read-all@example.com", institution.id)
    user = db_session.scalar(select(User).where(User.email == "read-all@example.com"))
    first = _published(db_session, institution, "First examination notice", key="https://university.example/first")
    second = _published(db_session, institution, "Second examination notice", key="https://university.example/second")
    db_session.add_all([
        StudentNotification(user_id=user.id, notice_id=first.id, kind="category_new_notice", title="First", message="First alert", dedupe_key="first"),
        StudentNotification(user_id=user.id, notice_id=second.id, kind="category_new_notice", title="Second", message="Second alert", dedupe_key="second"),
    ])
    db_session.commit()
    result = client.patch("/api/alerts/read-all", headers=_headers(token))
    assert result.status_code == 200
    assert result.json()["updated"] == 2
    assert client.get("/api/alerts/unread-count", headers=_headers(token)).json()["unread_count"] == 0
