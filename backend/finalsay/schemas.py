"""Shared Pydantic v2 request/response models."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

_EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def _validate_app_email(val: str) -> str:
    cleaned = str(val).strip().lower()
    if not _EMAIL_REGEX.match(cleaned):
        raise ValueError("Invalid email address format")
    return cleaned


AppEmail = Annotated[str, AfterValidator(_validate_app_email)]


class HealthResponse(BaseModel):
    status: str = "ok"


# --- Auth ---

class UserRegister(BaseModel):
    # No ``role`` field: public self-registration always creates a student
    # account. Privileged roles are provisioned only by trusted operator tools.
    email: AppEmail
    password: str = Field(min_length=6)
    display_name: str | None = None
    institution_id: int | None = None


class StudentInstitutionUpdate(BaseModel):
    institution_id: int


class UserLogin(BaseModel):
    email: AppEmail
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: AppEmail
    role: str
    display_name: str | None = None
    institution_id: int | None = None


class InstitutionChoiceOut(BaseModel):
    id: int
    name: str
    current_notice_count: int = 0


# --- Provenance ---

class MerkleBuildRequest(BaseModel):
    day: str = Field(description="Day to build the Merkle root for (YYYY-MM-DD).")


class MerkleBuildResponse(BaseModel):
    root_id: int
    day: str
    root_hash: str
    anchor_kind: str
    anchor_ref: str | None = None
    anchor_chain_id: int | None = None
    anchor_block_number: int | None = None
    anchor_network: str | None = None
    anchor_contract_address: str | None = None
    anchor_issuer_address: str | None = None
    anchor_issuer_id: str | None = None
    anchor_batch_id: str | None = None
    anchor_timestamp: datetime | None = None
    anchor_submitted_at: datetime | None = None
    anchor_status: str = "local"
    anchor_error: str | None = None
    leaf_count: int


class VerifyResponse(BaseModel):
    ok: bool
    tamper: bool
    integrity_status: str
    details: dict


# --- Comparison ---

class NoticeDocumentExtractionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_url: str
    extracted_text: str
    extracted_text_sha256: str
    extraction_quality: str
    extraction_method: str
    extracted_at: datetime

class ComparisonResponse(BaseModel):
    submission_id: int
    candidate_id: int | None = None
    edge_id: int
    label: str
    confidence: float
    rationale: str
    model: str
    status: str
    gated: bool
    review_case_id: int | None = None
    candidate_score: float | None = None
    candidate_institution: str | None = None
    candidate_issuer: str | None = None
    candidate_notice_date: str | None = None
    candidate_deadline: str | None = None
    candidate_source_url: str | None = None
    candidate_text: str | None = None
    candidate_supplemental_extractions: list["NoticeDocumentExtractionOut"] = []
    evidence: list[str] = []
    student_summary: str | None = None
    student_verdict: Literal[
        "CONFIRMED", "CONTRADICTED", "PARTIALLY_SUPPORTED",
        "NOT_FOUND", "NEEDS_MANUAL_VERIFICATION",
    ] = "NEEDS_MANUAL_VERIFICATION"


# --- Notices ---

class NoticeFieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    field_name: str
    value: str | None = None
    confidence: float | None = None


class NoticeSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    institution_id: int | None = None
    title: str | None = None
    published_date: str | None = None
    published_on: date | None = None
    issuer: str | None = None
    notice_date: str | None = None
    deadline: str | None = None
    audience: str | None = None
    source_url: str | None = None
    source_key: str | None = None
    extraction_quality: str = "unknown"
    source_state: str = "active"
    source_last_seen_at: datetime | None = None
    source_removed_at: datetime | None = None
    admin_status: str = "active"
    version_no: int = 1
    is_current: bool = True
    previous_version_id: int | None = None
    sha256: str | None = None
    retrieved_at: datetime | None = None


class NoticeDetail(NoticeSummary):
    submission_mode: Literal["claim", "document", "legacy"] = "legacy"
    action: str | None = None
    redacted_text: str | None = None
    fields: list[NoticeFieldOut] = []
    supplemental_extractions: list[NoticeDocumentExtractionOut] = []


class StudentNoticeItem(NoticeSummary):
    preview: str | None = None


class NoticeMonthCount(BaseModel):
    month: str
    count: int


class FollowedNoticeOut(BaseModel):
    notice: NoticeSummary
    followed_at: datetime


class CategoryFollowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    institution_id: int
    category_key: str
    category_label: str
    created_at: datetime


class CategoryFollowCreate(BaseModel):
    category_label: str = Field(min_length=2, max_length=120)


class StudentNotificationOut(BaseModel):
    id: int
    kind: str
    title: str
    message: str
    notice_id: int | None = None
    previous_notice_id: int | None = None
    notice_title: str | None = None
    previous_notice_title: str | None = None
    source_url: str | None = None
    created_at: datetime
    read_at: datetime | None = None
    old_value: str | None = None
    new_value: str | None = None


class StudentNotificationPage(BaseModel):
    items: list[StudentNotificationOut]
    total: int
    unread_count: int
    page: int
    page_size: int


class StudentFollowsOut(BaseModel):
    notices: list[FollowedNoticeOut]
    categories: list[CategoryFollowOut]


class StudentNoticeFeed(BaseModel):
    institution_id: int
    institution_name: str
    items: list[StudentNoticeItem]
    total: int
    page: int
    page_size: int
    page_count: int
    months: list[NoticeMonthCount]
    undated_count: int


class CandidateOut(BaseModel):
    notice: NoticeSummary
    score: float
    semantic_similarity: float | None = None


# --- Ingestion ---

class SubmitTextRequest(BaseModel):
    text: str = Field(min_length=1)
    institution_id: int | None = None


class FetchResponse(BaseModel):
    created: int
    modified: int = 0
    skipped: int
    removed: int = 0
    errors: int = 0
    institutions: list[dict]


class ScrapeScheduleOut(BaseModel):
    interval_seconds: int
    status: str = "idle"
    last_attempted_at: datetime | None = None
    last_completed_at: datetime | None = None
    next_scheduled_at: datetime | None = None
    last_error: str | None = None


class ScrapeScheduleUpdate(BaseModel):
    interval_seconds: int = Field(ge=1_800, le=21_600, multiple_of=300)


class AdminNoticeOut(NoticeSummary):
    redacted_text: str | None = None
    admin_review_note: str | None = None
    admin_reviewed_at: datetime | None = None
    admin_reviewed_by: int | None = None


class AdminNoticePage(BaseModel):
    items: list[AdminNoticeOut]
    total: int
    page: int
    page_size: int


class AdminNoticeReviewUpdate(BaseModel):
    status: Literal["active", "flagged", "archived", "rejected"]
    note: str | None = Field(default=None, max_length=1_000)


class AdminOfficialNoticeWrite(BaseModel):
    institution_id: int = Field(ge=1)
    title: str = Field(min_length=3, max_length=500)
    text: str = Field(min_length=1, max_length=100_000)
    source_url: str | None = Field(default=None, max_length=500)
    published_date: str | None = Field(default=None, max_length=80)


class AdminOfficialNoticeRevision(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=500)
    text: str = Field(min_length=1, max_length=100_000)
    source_url: str | None = Field(default=None, max_length=500)
    published_date: str | None = Field(default=None, max_length=80)


class InstitutionCreate(BaseModel):
    name: str
    slug: str
    source_url: str | None = None
    scrape_type: str = "generic_html"
    scrape_enabled: bool = False
    active: bool = True


class InstitutionUpdate(BaseModel):
    name: str | None = None
    source_url: str | None = None
    scrape_type: str | None = None
    scrape_enabled: bool | None = None
    active: bool | None = None


class InstitutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    source_url: str | None = None
    scrape_type: str
    scrape_enabled: bool
    active: bool
    backfill_complete: bool = False
    backfill_cursor: str | None = None
    last_successful_fetch: datetime | None = None
    last_successful_created: int = 0
    last_successful_modified: int = 0
    last_successful_unchanged: int = 0
    current_notice_count: int = 0
    extracted_count: int = 0
    listing_only_count: int = 0
    unextractable_count: int = 0
    unknown_extraction_count: int = 0


class IssuerMembershipCreate(BaseModel):
    issuer_email: AppEmail
    institution_id: int


class IssuerMembershipOut(BaseModel):
    user_id: int
    issuer_email: AppEmail
    display_name: str | None = None
    institution_id: int
    institution_name: str
    institution_slug: str
    active: bool


class ScrapeRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    institution_id: int
    status: str
    found: int
    created: int
    modified: int
    unchanged: int
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


# --- Issuer ---

class IssuerPublishRequest(BaseModel):
    text: str = Field(min_length=1)
    institution_id: int | None = None
    institution_slug: str | None = None
    source_url: str | None = None


# --- Reviewer ---

class ReviewQueueItem(BaseModel):
    case_id: int
    edge_id: int
    status: str
    label: str
    confidence: float
    rationale: str | None = None
    submission: NoticeSummary
    candidate: NoticeSummary | None = None


class ResolveRequest(BaseModel):
    label: str


class ResolveResponse(BaseModel):
    case_id: int
    edge_id: int
    label: str
    edge_status: str
    case_status: str


class BenchmarkPairOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    submission_id: int
    official_id: int
    submission_text: str | None = None
    official_text: str | None = None
    pair_kind: str = "student_comparison"
    institution_name: str | None = None
    left_title: str | None = None
    right_title: str | None = None
    your_label: str | None = None


class BenchmarkPairCreateRequest(BaseModel):
    """Admin curation links a real student submission to its actual candidate."""

    model_config = ConfigDict(extra="forbid")

    submission_id: int = Field(gt=0)
    official_id: int = Field(gt=0)


class BenchmarkPairCreateResponse(BaseModel):
    id: int
    submission_id: int
    official_id: int
    created: bool


class BenchmarkCandidateOut(BaseModel):
    """A real, persisted comparison eligible for blind benchmark curation."""

    submission_id: int
    official_id: int
    institution_name: str
    submission_title: str | None = None
    official_title: str | None = None
    submission_created_at: datetime
    official_published_date: str | None = None


class BenchmarkStatusOut(BaseModel):
    completed: bool
    pair_count: int
    annotated_count: int


class BenchmarkCompletionOut(BaseModel):
    completed: bool
    pair_count: int
    annotated_count: int


class BenchmarkAnnotateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair_id: int
    label: str


class KappaResponse(BaseModel):
    kappa: float | None = None
    ready: bool = False
    annotators: list[str]
    pairs_compared: int
    per_label: dict
    is_seed_demo: bool = False
