"""Comparison tests: the 22-vs-15 Sept case, confidence gating to unresolved +
review_case creation, and relation-edge persistence with rationale."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from finalsay.models import RelationEdge, ReviewCase, Notice
from finalsay.models_iface.comparison_model import (
    MockComparisonModel,
    get_comparison_model,
)
from finalsay.services import comparison


def test_default_comparison_model_is_mock():
    model = get_comparison_model()
    assert model.name == "mock"


def test_postponed_22_vs_15_sept_not_consistent():
    model = MockComparisonModel()
    label, confidence, rationale = model.classify(
        "The exam is on 15 Sept.",
        "The exam is postponed to 22 Sept.",
    )
    assert label in ("contradictory", "superseded"), (label, rationale)
    assert label != "consistent"
    assert 0.0 <= confidence <= 1.0
    assert rationale


def _make_notice(db, kind, text, institution_id=1, action=None, audience=None):
    notice = Notice(
        kind=kind,
        institution_id=institution_id,
        issuer="Registrar",
        redacted_text=text,
        action=action,
        audience=audience,
    )
    db.add(notice)
    db.flush()
    return notice


def test_low_confidence_yields_unresolved_and_review_case(db_session):
    official = _make_notice(
        db_session,
        "official",
        "Library will operate on holiday hours next week.",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "Cafeteria menu features seasonal vegetables.",
    )
    db_session.commit()

    # Deterministic low-confidence model to trigger gating.
    class _LowConf(MockComparisonModel):
        def classify(self, premise, hypothesis):
            return ("consistent", 0.3, "weak signal")

    outcome = comparison.classify_submission(
        db_session, submission, model=_LowConf(), threshold=0.6
    )
    db_session.commit()

    assert outcome.label == "unresolved"
    assert outcome.gated is True
    assert outcome.review_case is not None

    # A review_case row exists and points at the edge.
    cases = db_session.scalars(select(ReviewCase)).all()
    assert len(cases) == 1
    assert cases[0].edge_id == outcome.edge.id
    assert cases[0].status == "open"

    # The edge is persisted with rationale/model/status.
    edge = db_session.get(RelationEdge, outcome.edge.id)
    assert edge.label == "unresolved"
    assert edge.status == "unresolved"
    assert edge.model == "mock"
    assert edge.rationale
    assert "threshold" in edge.rationale.lower()


def test_model_unresolved_rationale_does_not_claim_high_score_is_below_threshold(db_session):
    official = _make_notice(
        db_session, "official", "The exam is scheduled on 15 Sept for all students."
    )
    submission = _make_notice(
        db_session, "submission", "The exam arrangements are described in this circular."
    )
    db_session.commit()

    class _Neutral(MockComparisonModel):
        def classify(self, premise, hypothesis):
            return ("unresolved", 0.99, "The model ranked neutral highest.")

    outcome = comparison.classify_submission(
        db_session, submission, model=_Neutral(), threshold=0.2
    )

    assert outcome.label == "unresolved"
    assert outcome.gated is True
    assert "routed to reviewer" in outcome.edge.rationale.lower()
    assert "below the threshold" not in outcome.edge.rationale.lower()


def test_ocr_course_heading_retrieves_exact_du_listing_over_semantic_decoy(db_session):
    target = _make_notice(
        db_session,
        "official",
        "Issued by: University of Delhi Examination\n"
        "Subject: Certificate Course in Tibetan Language\n"
        "Official listing: Certificate Course in Tibetan Language",
    )
    target.title = "Certificate Course in Tibetan Language"
    target.extraction_quality = "listing_only"
    target.published_on = date(2026, 10, 7)
    decoy = _make_notice(
        db_session,
        "official",
        "Notification regarding last date for filling up examination forms.",
    )
    decoy.title = "Notification regarding Last date for Filling up Examination form"
    decoy.published_on = date(2026, 11, 16)
    submission = _make_notice(
        db_session,
        "submission",
        "UNIVERSITY OF DELHI\n\n"
        "CERTIFICATE COURSE IN TIBETAN LANGUAGE EXAMINATION, 2026\n\n"
        "The following Candidates are declared to have Passed the Examination",
    )
    submission.title = None

    ranked = comparison.retrieve_candidates(db_session, submission, model=MockComparisonModel())

    assert submission.title == "CERTIFICATE COURSE IN TIBETAN LANGUAGE"
    assert ranked[0].notice.id == target.id
    assert ranked[0].title_similarity == 1.0
    assert ranked[1].notice.id == decoy.id


def _thapar_hostel_notice(db, title: str, body: str, published: str):
    notice = _make_notice(db, "official", body, institution_id=1)
    notice.title = title
    notice.published_date = published
    notice.extraction_quality = "extracted"
    return notice


def test_typed_hostel_claim_uses_relevant_retention_notice_not_semantic_decoy(db_session):
    relevant = _thapar_hostel_notice(
        db_session,
        "Hostel Retention Notice (EVEN Sem 2026-27)",
        """Date: 12 Nov 2026
NOTICE – Hostel Retention & Fee Payment for EVEN Semester 2026–27
UG / TSLAS 3rd Year
15 Nov to 25 Nov, 2026
21 Dec, 2026 to 2 Jan, 2027
""",
        "12 November 2026",
    )
    decoy = _thapar_hostel_notice(
        db_session,
        "Hostel Allotment for Odd Semester 2026–27",
        "Hostel allotment fee payment and room booking for UG 3rd Year students.",
        "03 March 2026",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "hostel fee payment deadline for ug 3rd year girls is 30 november",
        institution_id=1,
    )
    db_session.commit()

    class _SemanticDecoyFirst(MockComparisonModel):
        def rank_candidates(self, _submission, _officials):
            return [(decoy, 0.377, 0.593), (relevant, 0.320, 0.534)]

        def classify(self, _premise, _hypothesis):
            raise AssertionError("typed date claim should use schedule evidence")

    outcome = comparison.classify_submission(
        db_session, submission, model=_SemanticDecoyFirst(), threshold=0.6
    )
    db_session.commit()

    assert outcome.candidate is not None
    assert outcome.candidate.id == relevant.id
    assert outcome.edge.dst_notice_id == relevant.id
    assert outcome.label == "contradictory"
    assert outcome.student_verdict == "CONTRADICTED"
    assert outcome.confidence == 0.0
    assert outcome.edge.model == "claim-schedule-evidence"
    assert outcome.student_summary == (
        "The official notice lists 15 November 2026 to 25 November 2026 for "
        "UG / TSLAS 3rd Year; 30 November 2026 is outside that window, "
        "which ends on 25 November 2026. "
        "The notice does not specify a girls-only rule."
    )
    assert "03 March 2026" not in outcome.student_summary
    assert "publication date" in " ".join(outcome.evidence).lower()


def test_typed_deadline_claim_compares_schedule_not_publication_date(db_session):
    official = _thapar_hostel_notice(
        db_session,
        "Hostel Retention Notice (EVEN Sem 2026-27)",
        """Date: 12 Nov 2026
NOTICE – Hostel Retention & Fee Payment for EVEN Semester 2026–27
UG / TSLAS 3rd Year
15 Nov to 25 Nov, 2026
""",
        "12 November 2026",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "UG 3rd year hostel fee payment deadline is 25 November 2026",
        institution_id=1,
    )
    db_session.commit()

    class _Ranker(MockComparisonModel):
        def rank_candidates(self, _submission, officials):
            return [(officials[0], 0.8, 0.7)]

    outcome = comparison.classify_submission(db_session, submission, model=_Ranker())
    assert outcome.candidate.id == official.id
    assert outcome.label == "consistent"
    assert outcome.student_verdict == "CONFIRMED"
    assert "25 November 2026 is the last date" in (outcome.student_summary or "")
    assert "12 November 2026" not in (outcome.student_summary or "")


def test_explicit_claim_year_is_not_silently_replaced_by_notice_year(db_session):
    official = _thapar_hostel_notice(
        db_session,
        "Hostel Retention Notice (EVEN Sem 2026-27)",
        "NOTICE – Hostel Retention & Fee Payment for EVEN Semester 2026–27\n"
        "UG / TSLAS 3rd Year\n15 Nov to 25 Nov, 2026",
        "12 November 2026",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "UG 3rd year hostel fee payment deadline is 25 November 2027",
        institution_id=1,
    )
    assert comparison._claim_schedule_check(submission, official)[0] == "contradictory"
    assert "year does not match" in comparison._claim_schedule_check(submission, official)[1].lower()


def test_typed_deadline_claim_does_not_apply_schedule_from_wrong_semester_or_year(db_session):
    wrong = _thapar_hostel_notice(
        db_session,
        "Hostel Retention Notice (ODD Sem 2025-26)",
        "NOTICE – Hostel Retention & Fee Payment for ODD Semester 2025–26\n"
        "UG / TSLAS 3rd Year\n15 Nov to 25 Nov, 2025",
        "12 November 2025",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "EVEN semester 2026-27 UG 3rd year hostel fee payment deadline is 30 November",
        institution_id=1,
    )
    db_session.commit()

    assert comparison._claim_schedule_check(submission, wrong) == (None, None)


def test_claim_for_uncovered_student_category_is_not_given_a_deadline_verdict(db_session):
    official = _thapar_hostel_notice(
        db_session,
        "Hostel Retention Notice (EVEN Sem 2026-27)",
        "NOTICE – Hostel Retention & Fee Payment for EVEN Semester 2026–27\n"
        "UG / TSLAS 4th Year\n20 Nov to 30 Nov, 2026",
        "12 November 2026",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "UG 3rd year hostel fee payment deadline is 30 November 2026",
        institution_id=1,
    )
    assert comparison._claim_schedule_check(submission, official) == (None, None)


def test_deadline_claim_uses_end_of_category_window_not_any_date_inside_it(db_session):
    official = _thapar_hostel_notice(
        db_session,
        "Hostel Retention Notice (EVEN Sem 2026-27)",
        "NOTICE – Hostel Retention & Fee Payment for EVEN Semester 2026–27\n"
        "UG / TSLAS 4th Year\n20 Nov to 30 Nov, 2026",
        "12 November 2026",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "UG 4th year hostel retention deadline is 25 November 2026",
        institution_id=1,
    )
    assert comparison._claim_schedule_check(submission, official)[0] == "contradictory"


def test_gender_specific_claim_is_only_partially_supported_when_notice_omits_gender(db_session):
    official = _thapar_hostel_notice(
        db_session,
        "Hostel Retention Notice (EVEN Sem 2026-27)",
        "NOTICE – Hostel Retention & Fee Payment for EVEN Semester 2026–27\n"
        "UG / TSLAS 3rd Year\n15 Nov to 25 Nov, 2026",
        "12 November 2026",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "hostel retention deadline for UG 3rd year girls is 25 November",
        institution_id=1,
    )
    db_session.commit()
    outcome = comparison.classify_submission(db_session, submission, model=MockComparisonModel())
    assert outcome.label == "consistent"
    assert outcome.student_verdict == "PARTIALLY_SUPPORTED"
    assert "does not specify a girls-only rule" in (outcome.student_summary or "")


def test_unsupported_extension_rumour_is_not_asserted_as_false_or_confirmed(db_session):
    official = _thapar_hostel_notice(
        db_session,
        "Hostel Retention Notice (EVEN Sem 2026-27)",
        "NOTICE – Hostel Retention & Fee Payment for EVEN Semester 2026–27\n"
        "UG / TSLAS 3rd Year\n15 Nov to 25 Nov, 2026",
        "12 November 2026",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "hostel fees have been extended till 5 December",
        institution_id=1,
    )
    db_session.commit()

    class _NoModelGuess(MockComparisonModel):
        def rank_candidates(self, _submission, officials):
            return [(official, 0.7, 0.7)]

        def classify(self, _premise, _hypothesis):
            raise AssertionError("typed claims without direct evidence must not be guessed by NLI")

    outcome = comparison.classify_submission(db_session, submission, model=_NoModelGuess())
    assert outcome.label == "unresolved"
    assert outcome.student_verdict == "NEEDS_MANUAL_VERIFICATION"
    assert "No conclusion was made" in (outcome.student_summary or "")


def test_later_fee_extension_beats_original_schedule_and_supports_new_date(db_session):
    original = _thapar_hostel_notice(
        db_session,
        "Hostel Retention Notice (EVEN Sem 2026-27)",
        "NOTICE – Hostel Retention & Fee Payment for EVEN Semester 2026–27\n"
        "UG / TSLAS 3rd Year\n15 Nov to 25 Nov, 2026",
        "12 November 2026",
    )
    extension = _thapar_hostel_notice(
        db_session,
        "Extension of Hostel Retention Fee Payment (EVEN Sem 2026-27)",
        "NOTICE – Extension of Hostel Retention & Fee Payment for EVEN Semester 2026–27\n"
        "UG / TSLAS 3rd Year\nLast date for payment extended to 30 November 2026",
        "20 November 2026",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "hostel fee payment deadline for UG 3rd year is 30 November",
        institution_id=1,
    )
    db_session.commit()

    class _OriginalRanksFirst(MockComparisonModel):
        def rank_candidates(self, _submission, _officials):
            return [(original, 0.4, 0.65), (extension, 0.3, 0.58)]

    outcome = comparison.classify_submission(db_session, submission, model=_OriginalRanksFirst())
    assert outcome.candidate.id == extension.id
    assert outcome.label == "consistent"
    assert outcome.student_verdict == "CONFIRMED"
    assert "30 November 2026 is the last date" in (outcome.student_summary or "")


def test_unrelated_hostel_allotment_notice_is_not_a_deadline_candidate(db_session):
    decoy = _thapar_hostel_notice(
        db_session,
        "Hostel Allotment for Odd Semester 2026-27",
        "Hostel allotment fee payment and room booking for UG 3rd Year.\n"
        "Room booking period is 27 May to 6 June 2026.",
        "03 March 2026",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "hostel fee payment deadline for UG 3rd year is 30 November",
        institution_id=1,
    )
    db_session.commit()

    class _OnlyAllotment(MockComparisonModel):
        def rank_candidates(self, _submission, _officials):
            return [(decoy, 0.9, 0.9)]

    assert comparison.retrieve_candidates(db_session, submission, model=_OnlyAllotment()) == []


def test_high_confidence_persists_edge_without_review_case(db_session):
    official = _make_notice(
        db_session,
        "official",
        "The exam is scheduled on 15 Sept for all students.",
        action="exam on 15 Sept",
    )
    submission = _make_notice(
        db_session,
        "submission",
        "The exam is postponed to 22 Sept for all students.",
        action="exam postponed to 22 Sept",
    )
    submission.title = "Examination schedule notice"
    db_session.commit()

    outcome = comparison.classify_submission(db_session, submission, threshold=0.6)
    db_session.commit()

    assert outcome.candidate is not None
    assert outcome.candidate.id == official.id
    assert outcome.label in ("contradictory", "superseded")
    assert outcome.review_case is None

    edge = db_session.get(RelationEdge, outcome.edge.id)
    assert edge.src_notice_id == submission.id
    assert edge.dst_notice_id == official.id
    assert edge.status == "auto"
    assert edge.rationale


def test_no_candidate_routes_to_unresolved(db_session):
    # Submission with no official notices in its institution.
    submission = _make_notice(
        db_session, "submission", "Some standalone submission.", institution_id=99
    )
    db_session.commit()

    outcome = comparison.classify_submission(db_session, submission)
    db_session.commit()

    assert outcome.label == "unresolved"
    assert outcome.review_case is not None


def test_semantic_retrieval_uses_wrapped_subject_to_find_same_notice_listing(db_session):
    unrelated = _make_notice(
        db_session,
        "official",
        "LLB supplementary examination forms for May/June 2026.",
    )
    unrelated.title = (
        "Filing up Supplementary Examination form for students who appeared in VI Term LLB"
    )
    matching = _make_notice(
        db_session,
        "official",
        "Official listing: Last date for filling up examination forms for regular NCWEB & SOL, "
        "Semester I/III/V/VII/IX, Session December 2026-2027 is extended upto 15.11.2026.",
    )
    matching.title = (
        "Notification regarding Last date for Filling up Examination form for Regular, "
        "NCWEB & SOL students for Session December 2026-2027 is extended upto"
    )
    matching.extraction_quality = "unknown"
    submission = _make_notice(
        db_session,
        "submission",
        "‘Subject: Filling up Examination form for Regular, NCWEB & SOL students for\n"
        "‘Session December 2026-2027 Examination of date reg.\n\n"
        "The last date is extended upto 15.07.2028.",
    )

    class _ObservedSemanticRanker(MockComparisonModel):
        def rank_candidates(self, _submission, officials):
            # Reproduce the observed embedding order: the longer but unrelated
            # LLB PDF ranks above the correct short portal listing.
            return [
                (unrelated, 0.399, 0.833),
                (matching, 0.12, 0.54),
            ]

    ranked = comparison.retrieve_candidates(
        db_session, submission, model=_ObservedSemanticRanker()
    )

    assert ranked[0].notice.id == matching.id
    assert ranked[0].title_similarity >= 0.45
    assert submission.title.startswith("Filling up Examination form for Regular, NCWEB & SOL")


def test_same_topic_official_extension_precedes_older_original_schedule(db_session):
    from datetime import date

    original = _make_notice(
        db_session,
        "official",
        "The examination-form schedule applies to Regular, NCWEB and SOL students.",
    )
    original.title = (
        "Filling up Examination form for Regular, NCWEB & SOL students for "
        "Session December 2026-2027"
    )
    original.published_on = date(2026, 9, 1)

    update = _make_notice(
        db_session,
        "official",
        "Official listing: Last date for filling up examination forms is extended upto 15.11.2026.",
    )
    update.title = (
        "Notification regarding Last date for Filling up Examination form for Regular, "
        "NCWEB & SOL students for Session December 2026-2027 is extended upto"
    )
    update.published_on = date(2026, 10, 13)
    update.extraction_quality = "listing_only"

    submission = _make_notice(
        db_session,
        "submission",
        "Subject: Filling up Examination form for Regular, NCWEB & SOL students for "
        "Session December 2026-2027",
    )
    submission.title = "Filling up Examination form for Regular, NCWEB & SOL students for Session December 2026-2027"

    class _SemanticRanker(MockComparisonModel):
        def rank_candidates(self, _submission, _officials):
            # The original's longer PDF body gives it a slightly stronger
            # semantic rank, as occurred for the real DU records.
            return [(original, 0.7291, 0.88), (update, 0.7286, 0.83)]

    ranked = comparison.retrieve_candidates(
        db_session, submission, model=_SemanticRanker()
    )

    assert ranked[0].notice.id == update.id
    assert ranked[0].title_similarity >= 0.45
    assert ranked[1].notice.id == original.id


def test_listing_only_match_is_not_auto_classified_from_partial_evidence(db_session):
    listing = _make_notice(
        db_session,
        "official",
        "Official listing: Revised exam-form deadline for Regular NCWEB & SOL students.",
    )
    listing.title = "Revised exam-form deadline for Regular NCWEB & SOL students"
    listing.extraction_quality = "unknown"
    submission = _make_notice(
        db_session,
        "submission",
        "Subject: Revised exam-form deadline for Regular NCWEB & SOL students\n"
        "The last date is extended to 15 November 2026.",
    )
    submission.title = "Revised exam-form deadline for Regular NCWEB & SOL students"

    class _OverconfidentModel(MockComparisonModel):
        def rank_candidates(self, _submission, officials):
            return [(listing, 0.9, 0.95)]

        def classify(self, _premise, _hypothesis):
            return "extended", 0.96, "Extension cue detected."

    outcome = comparison.classify_submission(
        db_session, submission, model=_OverconfidentModel()
    )

    assert outcome.label == "unresolved"
    assert outcome.gated is True
    assert outcome.review_case is not None
    assert "portal listing" in outcome.edge.rationale.lower()


def test_conflicting_schedule_dates_override_high_confidence_consistent_label(db_session):
    official = _make_notice(
        db_session,
        "official",
        "The final deadline is 10 November 2026.\n"
        "Last date for filling the examination form is extended up to 15 November 2026.",
    )
    official.title = "Revised examination form deadline for December semester students"
    official.extraction_quality = "extracted"
    submission = _make_notice(
        db_session,
        "submission",
        "The final deadline is 10 November 2026.\n"
        "Last date for filling the examination form is extended up to 22 November 2026.",
    )
    submission.title = "Examination form deadline for December semester students"

    class _MisleadingSemanticModel(MockComparisonModel):
        def rank_candidates(self, _submission, _officials):
            return [(official, 0.99, 0.99)]

        def classify(self, _premise, _hypothesis):
            return "consistent", 0.99, "The shared notice scope is entailed."

    outcome = comparison.classify_submission(
        db_session, submission, model=_MisleadingSemanticModel()
    )

    assert outcome.label == "unresolved"
    assert outcome.gated is True
    assert outcome.review_case is not None
    assert "date safety check" in " ".join(outcome.evidence).lower()
    assert "dates conflict" in outcome.rationale.lower()


def test_explicit_official_revision_supersedes_matching_old_date(db_session):
    official = _make_notice(
        db_session,
        "official",
        "The End-Semester Examinations were previously scheduled to commence on "
        "30 November 2026. The examinations have been rescheduled and will now "
        "commence on 5 December 2026 instead.",
    )
    official.title = "Revised Schedule for End-Semester Examinations"
    official.extraction_quality = "extracted"
    submission = _make_notice(
        db_session,
        "submission",
        "The End-Semester Examinations will commence from 30 November 2026.",
    )
    submission.title = "End-Semester Examination Schedule"

    class _SharedOldDateTrap(MockComparisonModel):
        def rank_candidates(self, _submission, _officials):
            return [(official, 0.99, 0.99)]

        def classify(self, _premise, _hypothesis):
            # Models may select the identical old-date sentence and miss the
            # revised date elsewhere in the official notice.
            return "consistent", 0.99, "The shared old date is entailed."

    outcome = comparison.classify_submission(
        db_session, submission, model=_SharedOldDateTrap()
    )

    assert outcome.label == "superseded"
    assert outcome.gated is False
    assert outcome.review_case is None
    assert "explicitly revises" in outcome.rationale.lower()
    assert any("date safety check" in item.lower() for item in outcome.evidence)


def test_revised_title_alone_does_not_turn_date_conflict_into_superseded(db_session):
    official = _make_notice(
        db_session,
        "official",
        "The End-Semester Examinations will commence on 5 December 2026.",
    )
    official.title = "Revised Schedule for End-Semester Examinations"
    official.extraction_quality = "extracted"
    submission = _make_notice(
        db_session,
        "submission",
        "The End-Semester Examinations will commence on 30 November 2026.",
    )
    submission.title = "End-Semester Examination Schedule"

    class _DateConflictModel(MockComparisonModel):
        def rank_candidates(self, _submission, _officials):
            return [(official, 0.99, 0.99)]

        def classify(self, _premise, _hypothesis):
            return "consistent", 0.99, "The topic is related."

    outcome = comparison.classify_submission(
        db_session, submission, model=_DateConflictModel()
    )

    assert outcome.label == "unresolved"
    assert outcome.gated is True
    assert outcome.review_case is not None
    assert "routed to reviewer" in outcome.rationale.lower()


def test_unavailable_semantic_models_keep_candidate_and_route_to_review(db_session):
    from finalsay.models_iface.semantic_model import SemanticModelUnavailable

    official = _make_notice(
        db_session,
        "official",
        "Official listing: End-semester examination schedule for December 2026.",
    )
    official.title = "End-semester examination schedule December 2026"
    official.extraction_quality = "listing_only"
    submission = _make_notice(
        db_session,
        "submission",
        "Subject: End-semester examination schedule December 2026\n"
        "The exam has been rescheduled to 5 December 2026.",
    )
    submission.title = "End-semester examination schedule December 2026"

    class _UnavailableSemanticModel:
        name = "semantic-unavailable-test"

        def rank_candidates(self, _submission, _officials):
            raise SemanticModelUnavailable("Model files are offline.")

        def classify_with_evidence(self, _premise, _hypothesis):
            raise SemanticModelUnavailable("Model files are offline.")

    outcome = comparison.classify_submission(
        db_session, submission, model=_UnavailableSemanticModel()
    )

    assert outcome.candidate is not None
    assert outcome.candidate.id == official.id
    assert outcome.label == "unresolved"
    assert outcome.confidence == 0.0
    assert outcome.review_case is not None
    assert "could not be loaded" in outcome.rationale
