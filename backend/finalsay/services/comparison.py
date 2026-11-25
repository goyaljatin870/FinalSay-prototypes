"""Comparison (module 4, design.md section 7).

- Candidate retrieval: filter official notices by institution/audience when
  known, rank by token overlap, take top-K (K=5).
- ``classify_submission``: run the configured ComparisonModel against the best
  candidate, apply confidence gating (below ``FINALSAY_CONFIDENCE_THRESHOLD`` ->
  label forced to 'unresolved' and a ``review_case`` created), then persist a
  ``relation_edge`` with label, confidence, rationale, model name and status.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from finalsay.config import get_settings
from finalsay.models import Notice, NoticeDocumentExtraction, RelationEdge, ReviewCase
from finalsay.models_iface.comparison_model import ComparisonModel, get_comparison_model

TOP_K = 5
_REVISION_TITLE_RE = re.compile(
    r"\b(?:revised|revision|corrigendum|correction|corrected|amended|amendment|updated|update)\b",
    re.IGNORECASE,
)
_UPDATE_TITLE_RE = re.compile(
    r"\b(?:revised|revision|corrigendum|correction|corrected|amended|amendment|"
    r"updated|update|extended|extension|postponed|postponement|rescheduled|"
    r"cancelled|canceled|superseded)\b",
    re.IGNORECASE,
)


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _submission_text(notice: Notice) -> str:
    parts = [
        notice.title or "",
        notice.issuer or "",
        notice.action or "",
        notice.redacted_text or "",
        getattr(notice, "supplemental_extracted_text", "") or "",
    ]
    return "\n".join(p for p in parts if p)


_TITLE_TOPIC_STOPWORDS = {
    "a", "an", "and", "as", "by", "date", "dated", "examination", "exam",
    "extended", "extension", "filling", "for", "form", "from", "in", "is",
    "last", "notice", "notification", "of", "on", "or", "reg", "regarding",
    "subject", "student", "students", "the", "to", "up", "upto", "with",
}

_SEMESTER_RE = re.compile(r"\b(even|odd)\s*(?:semester|sem)\b", re.IGNORECASE)
_ACADEMIC_YEAR_RE = re.compile(r"\b(20\d{2})\s*[-–/]\s*(\d{2,4})\b")
_ACADEMIC_LEVEL_RE = re.compile(
    r"\b(1st|2nd|3rd|4th|first|second|third|fourth)(?:\s*-\s*|\s+)year\b",
    re.IGNORECASE,
)
_DEADLINE_INTENT_RE = re.compile(
    r"\b(deadline|due\s+date|last\s+date|payment\s+(?:due|closes|deadline)|"
    r"pay(?:ment)?\s+by|closes\s+on)\b", re.IGNORECASE
)
_DATE_RANGE_RE = re.compile(
    r"\b(?P<first>\d{1,2})\s+(?P<month1>[A-Za-z]{3,9})\s*"
    r"(?:to|through|[-–—])\s*(?P<second>\d{1,2})\s+(?P<month2>[A-Za-z]{3,9})"
    r"(?:\s*,?\s*(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)
_SAME_MONTH_RANGE_RE = re.compile(
    r"\b(?P<first>\d{1,2})\s*(?:to|through|[-–—])\s*"
    r"(?P<second>\d{1,2})\s+(?P<month>[A-Za-z]{3,9})"
    r"(?:\s*,?\s*(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)


def _likely_typed_claim(notice: Notice) -> bool:
    """Distinguish short sentence-like claims from pasted/uploaded notices."""
    if (notice.source_url or "").startswith("seed://"):
        return False
    submission_mode = getattr(notice, "submission_mode", "legacy")
    if submission_mode == "claim":
        return True
    if submission_mode == "document":
        return False
    text = notice.redacted_text or ""
    if len(text) > 900 or notice.title:
        return False
    if re.search(r"\b(?:notice|notification|circular|ref(?:erence)?\s*(?:no|number)|"
                 r"issued by|university|registrar|subject:)\b", text, re.IGNORECASE):
        return False
    return bool(re.search(
        r"\b(?:deadline|due|payment|fee|exam|hostel|application|scholarship|"
        r"admission|class|registration|rumou?r|heard|says|said)\b",
        text,
        re.IGNORECASE,
    ))


def _claim_context_score(submission: Notice, official: Notice) -> tuple[float, bool]:
    """Add domain, period, category and date-role signals to embedding rank.

    This is a ranking supplement, not a hard-coded institution rule. In
    particular, a relevant official title can rescue a candidate just below
    the embedding cutoff when the body contains the requested activity.
    """
    query = _submission_text(submission).casefold()
    content = "\n".join(
        part for part in (
            official.title or "", official.action or "", official.redacted_text or "",
            getattr(official, "supplemental_extracted_text", "") or "",
        ) if part
    ).casefold()
    title = (official.title or "").casefold()
    score = 0.0
    has_topic = False

    # Activity terms keep related but different processes (for example room
    # allotment vs retention) from tying solely on shared institution words.
    topic_groups = (
        ("hostel", ("hostel", "accommodation", "residential")),
        ("retention", ("retention", "retain", "checkout", "check-out", "vacated")),
        ("allotment", ("allotment", "allocation", "room booking")),
        ("fee", ("fee", "payment", "pay")),
        ("examination", ("examination", "exam", "timetable", "date sheet")),
        ("admission", ("admission", "enrolment", "enrollment", "application")),
        ("scholarship", ("scholarship", "fellowship", "stipend")),
    )
    query_groups = {key for key, terms in topic_groups if any(term in query for term in terms)}
    for key, terms in topic_groups:
        if key in query_groups and any(term in content for term in terms):
            score += 0.12
            has_topic = True

    if "hostel" in query_groups:
        query_retention = any(term in query for term in ("retention", "retain", "checkout", "check-out"))
        query_allotment = any(term in query for term in ("allotment", "allocation", "room booking"))
        if query_retention:
            score += 0.24 if any(term in title for term in ("retention", "retain", "checkout")) else 0
            if "allotment" in title and not query_allotment:
                score -= 0.20
        if query_allotment:
            score += 0.24 if any(term in title for term in ("allotment", "allocation", "room booking")) else 0
            if "retention" in title and not query_retention:
                score -= 0.20
        # If the claim asks about fees and the source title identifies a
        # retention/fee process, prefer it over a broad allotment circular.
        if "fee" in query_groups and "payment" in query and "retention" in title:
            score += 0.30
        if _UPDATE_TITLE_RE.search(title) and "fee" in query_groups and "hostel" in query_groups and "hostel" in title:
            # A newer extension/correction about the same fee process should
            # outrank the untouched original schedule when the claim is about
            # the current deadline.
            score += 0.34

    query_level = _ACADEMIC_LEVEL_RE.search(query)
    if query_level:
        level = query_level.group(1).casefold()
        alternatives = {
            "1st": ("1st", "first"), "first": ("1st", "first"),
            "2nd": ("2nd", "second"), "second": ("2nd", "second"),
            "3rd": ("3rd", "third"), "third": ("3rd", "third"),
            "4th": ("4th", "fourth"), "fourth": ("4th", "fourth"),
        }[level]
        found_level = any(re.search(rf"\b{word}\s+year\b", content) for word in alternatives)
        score += 0.14 if found_level else -0.12
        if re.search(r"\b(?:ug|undergraduate)\b", query):
            score += 0.10 if re.search(r"\b(?:ug|undergraduate)\b", content) else -0.08
        if re.search(r"\b(?:pg|postgraduate)\b", query):
            score += 0.10 if re.search(r"\b(?:pg|postgraduate)\b", content) else -0.08

    query_semester = _SEMESTER_RE.search(query)
    if query_semester:
        expected = query_semester.group(1).casefold()
        observed = {match.group(1).casefold() for match in _SEMESTER_RE.finditer(content)}
        score += 0.18 if expected in observed else (-0.30 if observed else -0.08)
    query_year = _ACADEMIC_YEAR_RE.search(query)
    if query_year:
        expected = query_year.group(0).replace("–", "-").replace("/", "-")
        observed_years = {
            match.group(0).replace("–", "-").replace("/", "-")
            for match in _ACADEMIC_YEAR_RE.finditer(content)
        }
        # Normalize 2026-27 and 2026-2027 to a common prefix.
        def year_key(value: str) -> str:
            parts = re.split(r"[-/]", value.replace("–", "-"))
            return f"{parts[0]}-{parts[1][-2:]}"
        score += 0.18 if any(year_key(value) == year_key(expected) for value in observed_years) else (-0.30 if observed_years else -0.08)

    query_dates = _dates_in_text(query)
    if _DEADLINE_INTENT_RE.search(query):
        has_relevant_deadline = bool(_dated_schedule_facts(official).get("deadline"))
        score += 0.10 if has_relevant_deadline else 0
        # A candidate containing the claimed date in its body is modest support;
        # the later claim verifier still checks its semantic role/category.
        if query_dates and any(date_value in _dates_in_text(content) for date_value in query_dates):
            score += 0.04

    return score, has_topic


def _dates_in_text(text: str) -> set[tuple[int, int, int | None]]:
    from finalsay.models_iface.semantic_model import _date_values

    return _date_values(text)


def _candidate_conflicts_with_claim(submission: Notice, official: Notice) -> bool:
    """Exclude candidates that conflict with explicit claim scope or process."""
    if not _likely_typed_claim(submission):
        return False
    claim = _submission_text(submission).casefold()
    content = "\n".join(
        part for part in (
            official.title or "", official.action or "", official.redacted_text or "",
            getattr(official, "supplemental_extracted_text", "") or "",
        ) if part
    ).casefold()
    title = (official.title or "").casefold()

    semester = _SEMESTER_RE.search(claim)
    if semester:
        observed = {match.group(1).casefold() for match in _SEMESTER_RE.finditer(content)}
        if observed and semester.group(1).casefold() not in observed:
            return True
    year = _ACADEMIC_YEAR_RE.search(claim)
    if year:
        def year_key(value: str) -> str:
            parts = re.split(r"[-/]", value.replace("–", "-"))
            return f"{parts[0]}-{parts[1][-2:]}"
        observed = {year_key(match.group(0)) for match in _ACADEMIC_YEAR_RE.finditer(content)}
        if observed and year_key(year.group(0)) not in observed:
            return True
    level = _ACADEMIC_LEVEL_RE.search(claim)
    if level:
        words = {
            "1st": ("1st", "first"), "first": ("1st", "first"),
            "2nd": ("2nd", "second"), "second": ("2nd", "second"),
            "3rd": ("3rd", "third"), "third": ("3rd", "third"),
            "4th": ("4th", "fourth"), "fourth": ("4th", "fourth"),
        }[level.group(1).casefold()]
        if re.search(r"\b(?:ug|undergraduate)\b", claim):
            scoped = re.search(r"\b(?:ug|undergraduate|tslas|sls)\b", content)
        elif re.search(r"\b(?:pg|postgraduate)\b", claim):
            scoped = re.search(r"\b(?:pg|postgraduate|phd)\b", content)
        else:
            scoped = None
        if scoped and not any(re.search(rf"\b{word}\s+year\b", content) for word in words):
            return True

    # A hostel payment deadline in a retention claim is not interchangeable
    # with the room-allotment fee/booking process.
    if (
        "hostel" in claim
        and "fee" in claim
        and _DEADLINE_INTENT_RE.search(claim)
        and "allotment" in title
        and "retention" not in title
        and not re.search(r"\b(?:allotment|allocation|room booking)\b", claim)
    ):
        return True
    return False


def _category_deadline_evidence(notice: Notice, claim: str) -> tuple[str | None, set[tuple[int, int, int | None]]]:
    """Find schedule dates adjacent to the claimed study-year category."""
    level_match = _ACADEMIC_LEVEL_RE.search(claim)
    if not level_match:
        return None, set()
    alternatives = {
        "1st": ("1st", "first"), "first": ("1st", "first"),
        "2nd": ("2nd", "second"), "second": ("2nd", "second"),
        "3rd": ("3rd", "third"), "third": ("3rd", "third"),
        "4th": ("4th", "fourth"), "fourth": ("4th", "fourth"),
    }[level_match.group(1).casefold()]
    body = "\n".join(part for part in (
        notice.title or "", notice.action or "", notice.redacted_text or "",
        getattr(notice, "supplemental_extracted_text", "") or "",
    ) if part)
    lines = [re.sub(r"\s+", " ", line).strip() for line in body.splitlines()]
    category_lines = [
        index for index, line in enumerate(lines)
        if any(re.search(rf"\b{word}\s+year\b", line, re.IGNORECASE) for word in alternatives)
    ]
    if not category_lines:
        return None, set()

    wants_ug = bool(re.search(r"\b(?:ug|undergraduate)\b", claim, re.IGNORECASE))
    wants_pg = bool(re.search(r"\b(?:pg|postgraduate)\b", claim, re.IGNORECASE))
    best: tuple[int, str, set[tuple[int, int, int | None]]] | None = None
    for index in category_lines:
        category_line = lines[index]
        if wants_ug and not re.search(r"\b(?:ug|undergraduate)\b", category_line, re.IGNORECASE):
            # Permit combined UG/TSLAS labels, but not PG-only table rows.
            if not re.search(r"\b(?:ug|tslas|sls)\b", category_line, re.IGNORECASE):
                continue
        if wants_pg and not re.search(r"\b(?:pg|postgraduate|phd)\b", category_line, re.IGNORECASE):
            continue
        dates: set[tuple[int, int, int | None]] = set()
        end = min(len(lines), index + 7)
        for later in range(index, end):
            if later > index and re.search(
                r"\b(?:\d(?:st|nd|rd|th)|first|second|third|fourth)\s+year\b|"
                r"\b(?:pg|postgraduate|phd)\b", lines[later], re.IGNORECASE,
            ):
                break
            ranges = list(_DATE_RANGE_RE.finditer(lines[later]))
            if ranges:
                # The first date range in the matched row is the primary
                # retention/payment window; e.g. the separate mess fee column
                # follows it and is not used for a hostel-fee claim.
                match = ranges[0]
                from finalsay.models_iface.semantic_model import _MONTHS
                m1 = _MONTHS.get(match.group("month1").lower())
                m2 = _MONTHS.get(match.group("month2").lower())
                if m1 and m2:
                    year = int(match.group("year")) if match.group("year") else None
                    dates.update({(m1, int(match.group("first")), year), (m2, int(match.group("second")), year)})
                    break
            else:
                compact_range = _SAME_MONTH_RANGE_RE.search(lines[later])
                if compact_range:
                    from finalsay.models_iface.semantic_model import _MONTHS
                    month = _MONTHS.get(compact_range.group("month").lower())
                    if month:
                        year = int(compact_range.group("year")) if compact_range.group("year") else None
                        dates.update({(month, int(compact_range.group("first")), year), (month, int(compact_range.group("second")), year)})
                        break
                # Also accept compact official schedules such as
                # ``15–25 November 2026`` or numeric DMY ranges.
                parsed_dates = sorted(_dates_in_text(lines[later]))
                if len(parsed_dates) >= 2:
                    if parsed_dates[-1][0] - parsed_dates[0][0] > 6:
                        parsed_dates.reverse()
                    dates.update(parsed_dates[:2])
                    break
                if len(parsed_dates) == 1 and re.search(
                    r"\b(?:deadline|due|last date|extended|extension|until|upto|up to|till|by)\b",
                    lines[later], re.IGNORECASE,
                ):
                    dates.add(parsed_dates[0])
                    break
        if dates:
            specificity = 2 if (wants_ug and re.search(r"\bug\b", category_line, re.I)) or (wants_pg and re.search(r"\bpg\b", category_line, re.I)) else 1
            candidate = (specificity, category_line, dates)
            if best is None or candidate[0] > best[0]:
                best = candidate
    if best is None:
        return None, set()
    return best[1], best[2]


def _claim_schedule_check(submission: Notice, official: Notice) -> tuple[str | None, str | None]:
    """Return an evidence-based verdict and short student-facing explanation."""
    if not _likely_typed_claim(submission):
        return None, None
    claim = submission.redacted_text or ""
    if not _DEADLINE_INTENT_RE.search(claim):
        return None, None
    official_text = "\n".join((official.title or "", official.redacted_text or "", official.action or ""))
    semester = _SEMESTER_RE.search(claim)
    if semester:
        source_semesters = {match.group(1).casefold() for match in _SEMESTER_RE.finditer(official_text)}
        if source_semesters and semester.group(1).casefold() not in source_semesters:
            return None, None
    academic_year = _ACADEMIC_YEAR_RE.search(claim)
    if academic_year:
        expected_parts = re.split(r"[-/]", academic_year.group(0).replace("–", "-"))
        expected_key = f"{expected_parts[0]}-{expected_parts[1][-2:]}"
        source_keys = set()
        for match in _ACADEMIC_YEAR_RE.finditer(official_text):
            parts = re.split(r"[-/]", match.group(0).replace("–", "-"))
            source_keys.add(f"{parts[0]}-{parts[1][-2:]}")
        if source_keys and expected_key not in source_keys:
            return None, None
    category, official_dates = _category_deadline_evidence(official, claim)
    claimed_dates = _dates_in_text(claim)
    if not category or not official_dates or not claimed_dates:
        return None, None
    # For unqualified dates, compare month/day; the official schedule supplies
    # the year. Publication/reference dates never enter this category row.
    claim_md = {(month, day) for month, day, _year in claimed_dates}
    official_md = {(month, day) for month, day, _year in official_dates}
    if len(claim_md) != 1:
        return None, None
    claim_month_day = next(iter(claim_md))
    ordered_window = sorted(official_md)
    start = ordered_window[0]
    end = ordered_window[-1]
    # A schedule window can cross a year boundary (for example 21 December to
    # 2 January). Treat a large month wrap as a year boundary.
    if len(ordered_window) == 2 and end[0] - start[0] > 6:
        start, end = end, start
    within_window = (
        start <= claim_month_day <= end
        if start[0] <= end[0]
        else claim_month_day >= start or claim_month_day <= end
    )
    from finalsay.models_iface.semantic_model import _MONTHS
    month_names = {month: name.title() for name, month in _MONTHS.items() if len(name) > 3}
    ordered_dates = sorted(official_dates)
    if len(ordered_dates) == 2 and ordered_dates[1][0] - ordered_dates[0][0] > 6:
        ordered_dates.reverse()
    observed = " to ".join(
        f"{day} {month_names.get(month, str(month))}{f' {year}' if year else ''}"
        for month, day, year in ordered_dates
    )
    category_scope = "The notice does not specify a girls-only rule." if re.search(r"\bgirls?\b", claim, re.I) and not re.search(r"\bgirls?\b", official.redacted_text or "", re.I) else ""
    claim_month, claim_day = claim_month_day
    year_by_date = {
        (month, day): year
        for month, day, year in official_dates
        if year is not None
    }
    explicit_claim_years = {year for _month, _day, year in claimed_dates if year is not None}
    explicit_claim_year = next(iter(explicit_claim_years)) if len(explicit_claim_years) == 1 else None
    official_final_year = year_by_date.get(end)
    claim_year = explicit_claim_year or year_by_date.get(claim_month_day) or official_final_year
    claim_date_text = f"{claim_day} {month_names.get(claim_month, str(claim_month))}{f' {claim_year}' if claim_year else ''}"
    final_date = f"{end[1]} {month_names.get(end[0], str(end[0]))}{f' {official_final_year}' if official_final_year else ''}"
    correct_year = not explicit_claim_year or not official_final_year or explicit_claim_year == official_final_year
    if claim_month_day == end and correct_year:
        summary = f"The official notice lists {observed} for {category}. {claim_date_text} is the last date in that window. {category_scope}".strip()
        return "consistent", summary
    if claim_month_day == end and not correct_year:
        summary = f"The official notice lists {final_date} as the last date for {category}; your claim says {claim_date_text}. The year does not match. {category_scope}".strip()
        return "contradictory", summary
    if within_window:
        summary = f"The official notice lists {observed} for {category}; its final date is {final_date}, not {claim_date_text}. {category_scope}".strip()
    else:
        summary = f"The official notice lists {observed} for {category}; {claim_date_text} is outside that window, which ends on {final_date}. {category_scope}".strip()
    return "contradictory", summary


def _title_topic_similarity(left: str | None, right: str | None) -> float:
    """Measure shared, discriminative title terms without generic notice words."""
    left_tokens = {
        token for token in re.findall(r"[a-z0-9]+", (left or "").casefold())
        if len(token) > 1 and token not in _TITLE_TOPIC_STOPWORDS
    }
    right_tokens = {
        token for token in re.findall(r"[a-z0-9]+", (right or "").casefold())
        if len(token) > 1 and token not in _TITLE_TOPIC_STOPWORDS
    }
    if not left_tokens or not right_tokens:
        return 0.0
    # Overlap coefficient tolerates a concise portal title matching a longer
    # subject heading while still penalizing unrelated audiences or semesters.
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def _has_listing_only_evidence(notice: Notice) -> bool:
    """Return true when a match has no underlying source document text."""
    if getattr(notice, "supplemental_extracted_text", ""):
        return False
    if notice.extraction_quality in {"listing_only", "unextractable"}:
        return True
    return bool(
        notice.extraction_quality == "unknown"
        and re.search(r"^\s*official listing\s*:", notice.redacted_text or "", re.IGNORECASE | re.MULTILINE)
    )


def _dated_schedule_facts(notice: Notice) -> dict[str, set[tuple[int, int, int | None]]]:
    """Extract dates attached to comparable schedule/deadline statements.

    Publication and reference dates are intentionally ignored. This is a
    conservative integration guard for the dangerous case where the semantic
    model selects a shared scope sentence and misses conflicting dates in the
    actual schedule rows.
    """
    from finalsay.models_iface.semantic_model import _date_values

    facts: dict[str, set[tuple[int, int, int | None]]] = {}
    text = "\n".join(
        part
        for part in (
            notice.title,
            notice.action,
            notice.redacted_text or "",
            # A source PDF may be attached to a sparse portal listing. Include
            # those separately extracted pages in the safety check as well as
            # in semantic classification.
            getattr(notice, "supplemental_extracted_text", "") or "",
        )
        if part
    )
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    metadata_line = re.compile(
        r"^(?:published(?:\s+date)?|publication\s+date|ref(?:erence)?\s*(?:no|number)\b|dated\b|date\s*:)",
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        if not line:
            continue
        if metadata_line.match(line):
            continue
        # PDF table extraction and OCR commonly put the row label on one line
        # and its value on the next. Inspect a narrow adjacent-line window,
        # while retaining line boundaries so dotted dates remain parseable.
        context = " ".join(
            adjacent
            for adjacent in lines[max(0, index - 1): min(len(lines), index + 2)]
            if adjacent and not metadata_line.match(adjacent)
        )
        lowered = context.casefold()
        if re.search(r"\b(?:last date|deadline|due date|extended|extension|late fee)\b", lowered):
            key = "deadline"
        elif re.search(r"\b(?:commence|commencing|begins?|starts?|scheduled|will be held|take place)\b", lowered):
            key = "event"
        else:
            continue
        dates = _date_values(context)
        if dates:
            facts.setdefault(key, set()).update(dates)
    return facts


def _schedule_date_conflicts(
    left: Notice, right: Notice
) -> dict[str, tuple[set[tuple[int, int, int | None]], set[tuple[int, int, int | None]]]]:
    """Return category-specific schedule dates when both notices disagree."""
    left_facts = _dated_schedule_facts(left)
    right_facts = _dated_schedule_facts(right)
    return {
        key: (left_facts[key], right_facts[key])
        for key in left_facts.keys() & right_facts.keys()
        if left_facts[key] != right_facts[key]
    }


_OFFICIAL_REVISION_RE = re.compile(
    r"\b(?:postpon(?:ed|ement)?|reschedul(?:ed|ing)?|revised|revision|"
    r"new\s+date|instead\s+of|previously\s+scheduled|earlier\s+date|"
    r"supersed(?:e|es|ed|ing)|replac(?:e|es|ed|ing)\s+by|amend(?:ed|ment))\b",
    re.IGNORECASE,
)


def _official_revision_explains_conflict(
    official: Notice,
    submission: Notice,
    conflicts: dict[str, tuple[set[tuple[int, int, int | None]], set[tuple[int, int, int | None]]]],
) -> bool:
    """Check whether a readable current source explicitly revises submitted facts.

    A notice can mention the old and new schedule in separate sentences. NLI
    may select the shared old-date sentence and call the documents consistent;
    this guard uses the whole official body but only converts to superseded
    when there is both explicit revision language and conflicting schedule
    facts. A revision-style title alone is not sufficient evidence.
    """
    official_text = "\n".join(
        part for part in (
            official.action,
            official.redacted_text,
            getattr(official, "supplemental_extracted_text", ""),
        ) if part
    )
    official_body = "\n".join(
        line for line in official_text.splitlines()
        if not re.match(r"^\s*(?:subject|title)\s*:", line, re.IGNORECASE)
    )
    if not _OFFICIAL_REVISION_RE.search(official_body):
        return False

    for official_dates, submitted_dates in conflicts.values():
        if submitted_dates and official_dates - submitted_dates:
            return True
    return False


def _explicit_revision_title_match(submission: Notice, official: Notice) -> bool:
    """Prefer a portal's clearly marked revision over its original same-topic notice."""
    submission_title = submission.title or ""
    official_title = official.title or ""
    submission_base = _REVISION_TITLE_RE.sub(" ", submission_title)
    official_base = _REVISION_TITLE_RE.sub(" ", official_title)
    def normalize(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))

    return bool(
        normalize(submission_title)
        and _REVISION_TITLE_RE.search(official_title)
        and not _REVISION_TITLE_RE.search(submission_title)
        and normalize(submission_base)
        and normalize(submission_base) == normalize(official_base)
    )


def _revision_first(submission: Notice, candidates: list[Candidate]) -> list[Candidate]:
    """Prefer an explicit same-topic update to an older original notice.

    Portal titles often say "extended" or "postponed" instead of "revised".
    Such a notice should be considered before the original when its subject is
    demonstrably the same. A revision cue alone is not enough: the title-topic
    overlap prevents unrelated updates from jumping ahead of a stronger match.
    """

    def update_match(item: Candidate) -> bool:
        title_similarity = item.title_similarity or _title_topic_similarity(
            submission.title, item.notice.title
        )
        return bool(
            title_similarity >= 0.45
            and _UPDATE_TITLE_RE.search(item.notice.title or "")
        )

    def publication_order(item: Candidate) -> int:
        published_on = item.notice.published_on
        return published_on.toordinal() if published_on is not None else 0

    return sorted(
        candidates,
        key=lambda item: (
            _explicit_revision_title_match(submission, item.notice),
            update_match(item),
            # A later publication date is meaningful only after topic relevance
            # and retrieval score. Previously it sorted every candidate by date
            # before score, so a recent unrelated exam notice could beat an old
            # exact-title match by months.
            item.title_similarity or _title_topic_similarity(
                submission.title, item.notice.title
            ),
            item.score,
            publication_order(item),
        ),
        reverse=True,
    )


@dataclass
class Candidate:
    notice: Notice
    score: float
    semantic_similarity: float | None = None
    title_similarity: float = 0.0


def retrieve_candidates(
    db: Session, submission: Notice, top_k: int = TOP_K,
    model: ComparisonModel | None = None,
) -> list[Candidate]:
    """Return top-K relevant official notices using semantic or lexical ranking."""
    if not submission.title and submission.redacted_text:
        # Older student submissions may predate the OCR-safe wrapped-subject
        # parser; recover the title from their already-redacted stored text.
        from finalsay.services.ingestion import _extract_title

        submission.title = _extract_title(submission.redacted_text)

    query = select(Notice).where(
        Notice.kind == "official",
        Notice.is_current.is_(True),
        Notice.admin_status == "active",
        Notice.source_state == "active",
        Notice.id != submission.id,
    )
    if submission.institution_id is not None:
        query = query.where(Notice.institution_id == submission.institution_id)

    officials = db.scalars(query).all()

    if officials:
        supplement_query = (
            select(
                NoticeDocumentExtraction.notice_id,
                NoticeDocumentExtraction.extracted_text,
            )
            .join(Notice, Notice.id == NoticeDocumentExtraction.notice_id)
            .where(
                Notice.kind == "official",
                Notice.is_current.is_(True),
                Notice.admin_status == "active",
                Notice.source_state == "active",
            )
        )
        if submission.institution_id is not None:
            supplement_query = supplement_query.where(
                Notice.institution_id == submission.institution_id
            )
        supplements: dict[int, list[str]] = {}
        for notice_id, text in db.execute(supplement_query):
            supplements.setdefault(notice_id, []).append(text)
        for official in officials:
            official.supplemental_extracted_text = "\n".join(
                supplements.get(official.id, [])
            )

    model = model or get_comparison_model()
    semantic_ranker = getattr(model, "rank_candidates", None)
    if semantic_ranker is not None:
        try:
            ranked = semantic_ranker(submission, officials)
        except Exception as exc:  # offline model weights: preserve retrieval via lexical fallback
            from finalsay.models_iface.semantic_model import SemanticModelUnavailable

            if not isinstance(exc, SemanticModelUnavailable):
                raise
            ranked = None
        if ranked is not None:
            minimum = get_settings().semantic_candidate_threshold
            title_scores = {
                notice.id: _title_topic_similarity(submission.title, notice.title)
                for notice, _rank_score, _similarity in ranked
            }
            strong_title_matches = any(score >= 0.45 for score in title_scores.values())
            candidates = []
            for notice, rank_score, similarity in ranked:
                if _candidate_conflicts_with_claim(submission, notice):
                    continue
                title_similarity = title_scores[notice.id]
                context_score, has_topic = (
                    _claim_context_score(submission, notice)
                    if _likely_typed_claim(submission) else (0.0, False)
                )
                # A typed claim can have weaker whole-document embedding
                # similarity than a related but wrong process. Keep a source
                # when its topic/category/date context is strong, even if it is
                # just below the generic embedding threshold.
                if similarity < minimum and title_similarity < 0.45 and not (has_topic and context_score >= 0.22):
                    continue
                if strong_title_matches and title_similarity < 0.25:
                    continue
                # Let an informative subject/title rescue a listing-only official
                # record whose sparse text gives it a weak embedding score.
                score = (
                    0.45 * rank_score + 0.55 * title_similarity
                    if title_similarity > 0
                    else rank_score
                )
                if _likely_typed_claim(submission):
                    score = max(0.0, score + context_score)
                candidates.append(
                    Candidate(
                        notice=notice,
                        score=score,
                        semantic_similarity=similarity,
                        title_similarity=title_similarity,
                    )
                )
            return _revision_first(submission, candidates)[:top_k]

    sub_tokens = _tokens(_submission_text(submission))
    aud_tokens = _tokens(submission.audience)

    scored: list[Candidate] = []
    for official in officials:
        if _candidate_conflicts_with_claim(submission, official):
            continue
        score = _overlap(sub_tokens, _tokens(_submission_text(official)))
        title_similarity = _title_topic_similarity(submission.title, official.title)
        score = 0.45 * score + 0.55 * title_similarity if title_similarity > 0 else score
        if _likely_typed_claim(submission):
            context_score, _has_topic = _claim_context_score(submission, official)
            score = max(0.0, score + context_score)
        if aud_tokens and _tokens(official.audience) & aud_tokens:
            score += 0.1  # small boost for matching audience
        scored.append(
            Candidate(notice=official, score=score, title_similarity=title_similarity)
        )

    scored.sort(key=lambda c: c.score, reverse=True)
    return _revision_first(submission, scored)[:top_k]


@dataclass
class ComparisonOutcome:
    edge: RelationEdge
    candidate: Notice | None
    label: str
    confidence: float
    rationale: str
    gated: bool
    review_case: ReviewCase | None
    evidence: list[str]
    candidate_score: float | None
    student_summary: str | None = None
    student_verdict: str = "NEEDS_MANUAL_VERIFICATION"


def student_verdict_for(
    submission: Notice,
    candidate: Notice | None,
    label: str,
    rationale: str | None = None,
) -> str:
    """Map internal notice relationships to cautious student-facing outcomes."""
    if candidate is None:
        return "NOT_FOUND"
    if _has_listing_only_evidence(candidate):
        return "NEEDS_MANUAL_VERIFICATION"
    if _likely_typed_claim(submission):
        if not (rationale or "").startswith("The official notice lists "):
            return "NEEDS_MANUAL_VERIFICATION"
        if label == "consistent" and "does not specify" in (rationale or "").casefold():
            return "PARTIALLY_SUPPORTED"
    if label == "consistent":
        return "CONFIRMED"
    if label == "contradictory":
        return "CONTRADICTED"
    if label in {"superseded", "corrected", "extended", "cancelled"}:
        return "CONTRADICTED"
    return "NEEDS_MANUAL_VERIFICATION"


def classify_submission(
    db: Session,
    submission: Notice,
    model: ComparisonModel | None = None,
    threshold: float | None = None,
) -> ComparisonOutcome:
    """Classify a submission against its best candidate and persist the edge.

    Applies confidence gating (GC-4): when confidence is below the threshold the
    label is forced to 'unresolved' and a review_case is created. The relation
    edge is always persisted with label, confidence, rationale, model and status.
    """
    settings = get_settings()
    model = model or get_comparison_model()
    threshold = settings.confidence_threshold if threshold is None else threshold

    evidence: list[str] = []
    student_summary: str | None = None
    student_verdict = "NEEDS_MANUAL_VERIFICATION"
    outcome_model_name = model.name
    try:
        candidates = retrieve_candidates(db, submission, model=model)
    except Exception as exc:  # model unavailable: preserve API flow as unresolved
        from finalsay.models_iface.semantic_model import SemanticModelUnavailable

        if not isinstance(exc, SemanticModelUnavailable):
            raise
        candidates = []
        evidence.append(str(exc))
    best = candidates[0].notice if candidates else None
    best_score = candidates[0].score if candidates else None
    if candidates and candidates[0].semantic_similarity is not None:
        evidence.append(
            "Candidate retrieval: combined embedding/lexical rank "
            f"{candidates[0].score:.3f}; embedding cosine "
            f"{candidates[0].semantic_similarity:.3f}."
        )
    elif candidates:
        evidence.append(
            "Candidate retrieval: lexical/title fallback; semantic model ranking was unavailable."
        )

    if best is None:
        # No sufficiently relevant official notice -> route to reviewer.
        label, confidence, rationale = (
            "unresolved",
            0.0,
            "No sufficiently relevant official notice was available for comparison.",
        )
        if evidence:
            rationale = f"Semantic comparison unavailable: {evidence[0]}"
        gated = True
        student_verdict = "NOT_FOUND"
    else:
        premise = _submission_text(best)
        hypothesis = _submission_text(submission)
        claim_label, claim_summary = _claim_schedule_check(submission, best)
        if claim_label and claim_summary:
            # For concise typed claims, compare the claimed activity/date with
            # the matching category's schedule row. Whole-notice NLI often
            # attends to publication or unrelated schedule dates instead.
            label, confidence, rationale = claim_label, 0.0, claim_summary
            outcome_model_name = "claim-schedule-evidence"
            student_summary = claim_summary
            gender_unconfirmed = "does not specify a girls-only rule" in claim_summary.casefold()
            student_verdict = (
                "CONTRADICTED" if claim_label == "contradictory"
                else "PARTIALLY_SUPPORTED" if gender_unconfirmed
                else "CONFIRMED"
            )
            evidence.append(f"Claim check: {claim_summary}")
            category, dates = _category_deadline_evidence(best, submission.redacted_text or "")
            if category and dates:
                from finalsay.models_iface.semantic_model import _MONTHS
                rendered = ", ".join(
                    f"{day} {next((name.title() for name, number in _MONTHS.items() if number == month and len(name) > 3), str(month))}{f' {year}' if year else ''}"
                    for month, day, year in sorted(dates)
                )
                evidence.append(f"Schedule evidence for {category}: {rendered}.")
            evidence.append(
                f"Publication date: {best.published_date or 'not available'}; it was not compared as a deadline."
            )
            gated = False
        elif _likely_typed_claim(submission):
            label, confidence = "unresolved", 0.0
            outcome_model_name = "claim-manual-review"
            rationale = (
                "A related official notice was found, but FinalSay could not verify the "
                "specific claim from a clearly applicable passage in the readable source. "
                "No conclusion was made about whether your information is correct."
            )
            student_summary = rationale
            claim_summary = rationale
            student_verdict = "NEEDS_MANUAL_VERIFICATION"
            evidence.append("Claim check: no sufficiently specific official evidence was identified for this claim.")
            gated = True
        else:
            sub_norm = re.sub(r"\s+", " ", submission.redacted_text or "").strip()
            best_norm = re.sub(r"\s+", " ", best.redacted_text or "").strip()
            has_contradict_words = bool(re.search(r"\b(cancel|postpon|reschedul|delay|extend|fake|rumou?r)", sub_norm.lower()))

            if sub_norm and best_norm and (sub_norm == best_norm or ((sub_norm in best_norm or best_norm in sub_norm) and not has_contradict_words)):
                label, confidence, rationale = (
                    "consistent",
                    0.99 if sub_norm == best_norm else 0.88,
                    "The submitted notice text matches the readable official notice." if sub_norm == best_norm else "The submitted notice text is contained in the readable official notice.",
                )
                gated = False
                student_verdict = "CONFIRMED"
                student_summary = "The text in your copy is the same as the readable official notice." if sub_norm == best_norm else "No important difference was found in the readable official notice."
                evidence.append("Exact match: submitted text matches the official notice." if sub_norm == best_norm else "Content match: submitted text is contained in the official notice.")
            else:
                detailed = getattr(model, "classify_with_evidence", None)
                try:
                    if detailed is not None:
                        label, confidence, rationale, model_evidence = detailed(premise, hypothesis)
                        evidence.extend(model_evidence)
                    else:
                        label, confidence, rationale = model.classify(premise, hypothesis)
                        evidence.append(rationale)
                except Exception as exc:  # model weights unavailable: return reviewable result, not 500
                    from finalsay.models_iface.semantic_model import SemanticModelUnavailable

                    if not isinstance(exc, SemanticModelUnavailable):
                        raise
                    label, confidence, rationale = (
                        "unresolved",
                        0.0,
                        "A likely official notice was found, but the local comparison model "
                        "could not be loaded. Routed to reviewer without asserting a label.",
                    )
                    evidence.append(str(exc))
        if not claim_summary:
            # Confidence gating (GC-4): never force a guess below the threshold.
            gated = confidence < threshold or label == "unresolved"
            if label == "unresolved":
                rationale = (
                    f"{rationale} The model did not support a resolved relationship; "
                    "routed to reviewer."
                )
                label = "unresolved"
            elif confidence < threshold:
                rationale = (
                    f"{rationale} Confidence {confidence:.2f} is below the "
                    f"threshold {threshold:.2f}; routed to reviewer."
                )
                label = "unresolved"

            schedule_conflicts = _schedule_date_conflicts(best, submission)
            if label == "consistent" and schedule_conflicts:
                for category, (official_dates, submitted_dates) in sorted(schedule_conflicts.items()):
                    evidence.append(
                        f"Date safety check ({category}): official notice contains "
                        f"{sorted(official_dates)}; submitted notice contains "
                        f"{sorted(submitted_dates)}."
                    )
                if _official_revision_explains_conflict(best, submission, schedule_conflicts):
                    label = "superseded"
                    rationale = (
                        "The current official notice explicitly revises a schedule "
                        "date in the submitted copy. The submitted notice is superseded."
                    )
                else:
                    rationale = (
                        "The model found the notices semantically similar, but their "
                        "schedule or deadline dates conflict. Routed to reviewer instead "
                        "of asserting that both notices are consistent."
                    )
                    label = "unresolved"
                    gated = True

        if _has_listing_only_evidence(best):
            evidence.append(
                "The likely official match is available only as a portal listing; its source document text was not retrieved."
            )
            student_verdict = "NEEDS_MANUAL_VERIFICATION"
            student_summary = (
                "A related official portal listing was found, but FinalSay could not read "
                "the original document. Its dates and instructions need manual checking. "
                "This is not a finding that your notice is false."
            )
            if label != "unresolved":
                rationale = (
                    "The closest same-topic official notice is only a portal listing, so "
                    "the relationship cannot be verified from the original document. "
                    "Routed to reviewer instead of applying the model's provisional label."
                )
                label = "unresolved"
            else:
                rationale = (
                    f"{rationale} The matching official record is only a portal listing, "
                    "so the source document could not be checked."
                )
            gated = True

        if student_verdict == "NEEDS_MANUAL_VERIFICATION":
            student_verdict = student_verdict_for(submission, best, label, rationale)

    status = "unresolved" if label == "unresolved" else "auto"
    edge = RelationEdge(
        src_notice_id=submission.id,
        dst_notice_id=best.id if best is not None else submission.id,
        label=label,
        confidence=confidence,
        rationale=rationale,
        model=outcome_model_name,
        status=status,
    )
    db.add(edge)
    db.flush()

    review_case: ReviewCase | None = None
    if label == "unresolved":
        review_case = ReviewCase(
            edge_id=edge.id,
            status="open",
            assigned_role="reviewer",
        )
        db.add(review_case)
        db.flush()

    return ComparisonOutcome(
        edge=edge,
        candidate=best,
        label=label,
        confidence=confidence,
        rationale=rationale,
        gated=gated,
        review_case=review_case,
        evidence=evidence,
        candidate_score=best_score,
        student_summary=student_summary,
        student_verdict=student_verdict,
    )
