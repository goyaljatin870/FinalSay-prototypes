# FinalSay Functional Verification Report

**Execution date:** 17 November 2026<br>
**Environment:** Local development app, `127.0.0.1:5173` frontend and `127.0.0.1:8000` FastAPI backend; existing development database preserved.<br>
**Scope:** Functional workflows, frontend/backend integration, database persistence, scraping, notice comparison, and focused authorization/error handling. This is not a production readiness or security certification.

## System examined

- **Frontend:** React + TypeScript + Vite application under `apps/web`; student pages cover registration/login, notice submission, official notice feed, alerts, and results. Admin screens cover source configuration and official notice review/status actions.
- **Backend:** FastAPI application under `backend/finalsay`; auth, ingestion/comparison, notice/candidate retrieval, scraping/scheduler, issuer publish, admin review, reviewer, and provenance APIs are registered in `finalsay/main.py`.
- **Persistence:** SQLAlchemy models and migrations; the running local app uses the existing SQLite database. PostgreSQL is supported by configuration for managed deployments.
- **Authentication and roles:** Bearer access token flow with student, issuer, reviewer, and admin roles. Privileged accounts are provisioned rather than self-selected at public registration.
- **Extraction/comparison:** PDF text extraction and optional OCR feed extracted/redacted notice text into candidate retrieval and comparison. The local app setting selects the semantic model; the automated test harness intentionally selects the deterministic mock for most tests, with semantic behavior separately exercised using the cached model weights.
- **Sources:** Configured institutional adapters poll official listings and persist source keys, revisions, dates, extraction status, and run outcomes. Test fixtures exist for isolated automated tests and are excluded from normal live-source fetches by default.
- **Incomplete workflow found:** Admin can review/flag/retain/archive/reject collected notices, but no admin API for creating, editing, or deleting official notice content was found. Issuers can publish through their separate membership-scoped API.

The pre-fix baseline suite and frontend build completed successfully before the targeted changes. Final regression evidence is recorded below.

## A. Test execution summary

Statuses use **PASS**, **PARTIAL**, and **FAIL**. PARTIAL means that a meaningful part was tested, but a required live dependency, workflow, or stress scenario could not be verified. No test is marked PASS solely because an endpoint returned success.

| Test Case | Description | Result | Remarks |
|---|---|---|---|
| TC-01 | Student registration | PASS | Valid registration completed in an isolated copy of the local database. Invalid email and duplicate registration validation were exercised; password persistence uses the existing hash flow. |
| TC-02 | Student login | PASS | Login, logout, protected access, and a saved result reloaded after logging out and back in. Empty and invalid credentials were rejected. |
| TC-03 | Student notice upload | PASS | API tests covered a real Thapar PDF, unsupported/corrupt/empty input, size limit, and blank/oversized pasted text. OCR was also run on the supplied Tibetan-language notice image; readable course/department text was extracted and sensitive values were redacted. |
| TC-04 | Notice verification | PASS | Controlled checks covered consistent, contradictory, unresolved, and explicit schedule-revision behavior. The real Tibetan-language student sample retrieved DU notice #384; because its Drive PDF could not be read in this environment, the result correctly remained “possible match” rather than claiming verified details. |
| TC-05 | Student verification history | PASS | A saved submission/result remained available after relogin; API ownership checks prevent a student from reading another user's submission. |
| TC-06 | Admin login and dashboard | PASS | Admin login redirected to `/admin/sources` in browser QA; the real Admin screen loaded source configuration, schedule, status, and run controls. Student-denial and privileged-role self-assignment checks passed through backend tests. |
| TC-07 | Admin official notice management | FAIL | The tested codebase does not provide complete admin add/update/delete management for official notice content. Issuer publishing exists separately; source-profile and review operations do not satisfy this test case. |
| TC-08 | Automatic website scraping | PARTIAL | A real scheduled cycle completed across all four institutions and persisted 26 new records. Manual polling was also triggered through Admin → Sources against an isolated copy of the database: the UI reported 15 new, 68 unchanged, and 2 source errors, with successful persistence in the clone. Thapar found 8 and made no changes. VTU and DU runs were partial because some attachments were unavailable/unextractable; Mumbai completed successfully. An external Thapar shortlink remains unsupported, and repeat execution across a full hourly interval was not observed. |
| TC-09 | Admin and student data consistency | PASS | Uploaded submission and result associations persisted in the isolated QA database; scraped official notices were candidates for retrieval. The live Thapar records are in the database used by the local API. |
| TC-10 | Error handling and edge cases | PARTIAL | Invalid registration, blank/long text, unsupported/empty/corrupt/oversized files, and OCR-unavailable handling were tested. Simultaneous uploads, process interruption mid-upload, and an induced live database outage were not stress-tested. |

**Totals:** 10 test cases executed; 7 PASS, 2 PARTIAL, 1 FAIL, 0 BLOCKED.<br>
**Automated regression suite:** 265 backend tests passed, 0 failed.<br>
**Frontend production build:** `npm run build` passed (TypeScript and Vite).<br>
**Local service check:** `GET /api/health` and `GET /api/health/ready` returned `{"status":"ok"}`; frontend returned HTTP 200.

### Per-case execution record

| Case | Preconditions | Steps performed | Expected | Actual and evidence |
|---|---|---|---|---|
| TC-01 | Isolated test database and local API test client. | Loaded registration form in browser QA; registered a student; repeated with malformed email, short password, and an existing email; inspected stored user password hash. | Valid student is created once, invalid data is rejected, password is not stored in plaintext. | **PASS.** Registration produced a student record, duplicate returned 409, malformed/short values returned 422, password hash verified through the application hash verifier. Browser registration completed against a disposable DB copy. |
| TC-02 | Student account plus isolated browser session. | Logged in with valid, invalid, and empty credentials; visited protected route; logged out; reopened saved result after login. | Valid login creates a usable session; invalid login is denied; logout removes access; saved result persists. | **PASS.** Valid login and persisted result route worked in browser QA; bad credentials returned 401, empty credentials 422, and unauthenticated identity access returned 401. |
| TC-03 | TestClient, supported Thapar PDF fixture, unsupported/corrupt files, and supplied notice image. | Submitted actual PDF and text; tried blank/overlong text, over-limit, empty/corrupt PDF, and unsupported format; ran OCR on the supplied screenshot. | Valid content extracts and associates with submitter; invalid/unreadable files fail clearly without server crash. | **PASS.** Valid PDF created a stored submission/result association; invalid files/text returned client errors (422/413). OCR extracted the Tibetan course heading and Buddhist Studies department from the supplied image and applied redaction. |
| TC-04 | Controlled official/student notice pairs; local semantic model snapshot; real DU sample and source record. | Tested consistent, conflicting, superseded schedule, title-only conflict and low-evidence cases; checked candidate order, persisted relation/review edge, and result page; submitted the real OCR sample in browser QA. | Correct candidate and comparison evidence are saved; revision is distinguished from consistency; insufficient evidence remains unresolved. | **PASS for exercised cases.** Controlled model/service tests covered the four labels and regression guard; revised schedule persisted as superseded, while title-only/partial listing cases stayed unresolved. Real browser sample retrieved #384 and showed a possible match because the PDF could not be retrieved. This does not establish universal accuracy. |
| TC-05 | Two student identities and saved submission records. | Read/recompared own submission; attempted another student's result/document; refreshed and re-opened own result after logout/login. | Own history is persistent; cross-user access is denied. | **PASS.** Ownership tests deny cross-user notice/result access (404) and recompare; browser result persisted after relogin. |
| TC-06 | Provisioned admin and student identities in isolated test environments. | Logged in through browser as a temporary admin on a DB clone and followed redirect to Admin → Sources; loaded current schedule, sources, run table, and poll controls. Backend tests covered valid/invalid credentials, student 403, admin access, and attempts to self-assign privileged roles. | Admin dashboard should load and expose admin functions; students should remain denied. | **PASS for tested dashboard/role paths.** The browser loaded `/admin/sources` as `QA admin (admin)` and showed schedule/source/run controls. Backend tests returned admin access and student 403; public role claims remained student. Admin notice-content CRUD is separately recorded as TC-07 FAIL. |
| TC-07 | Existing admin route inventory and issuer publish API. | Inspected admin notice API and UI; tested list/review status path; exercised issuer publishing and student denial. | Admin can add/update official notice metadata/content, validate it, avoid duplicates, and have updates affect future matching; deletion only if supported. | **FAIL for requested admin CRUD.** Admin UI/API can list and change review status, not add/edit/delete notice content. Separate issuer API successfully creates a new official notice with membership authorization, but that is not admin CRUD. |
| TC-08 | Configured live sources, local database, scheduler and Admin UI. | Checked the actual scheduled run state and source rows in the local database; signed into Admin → Sources in a browser using a temporary admin in a disposable DB snapshot; clicked **Poll configured sources** and verified the returned summary and cloned DB rows. | Poll controls work through Admin; current official notices persist once; source changes/errors are logged; scheduler executes and makes notices retrievable. | **PARTIAL.** A real scheduled cycle ran from `2026-11-17 15:12:37` to `15:15:55` UTC. It created 5 VTU, 1 DU and 20 Mumbai records; Thapar found 8, created/modified 0 and unchanged 8. Main API/database counts then showed VTU 885, DU 417, Mumbai 1383, Thapar 8. The browser Admin action returned HTTP 200 and displayed `Poll complete: 15 new, 0 revised, 68 unchanged, 0 removed, 2 source errors`; its database clone saved 5 VTU and 10 Mumbai records, with the same 8 Thapar notices unchanged. Clone counts moved from VTU 885 / DU 417 / Mumbai 1383 / Thapar 8 to 890 / 417 / 1393 / 8. VTU's new five are Jan 2021 backfill items with extracted text; DU's Nov 17 record is listing-only because Drive access is not configured; Mumbai's scheduled run added 10 extracted Nov 17 items and 10 extracted Aug 14 items, while the later clone UI poll added 10 more. VTU and DU run errors were persisted and displayed. The manual UI test changed only the disposable clone, not the user's main database. Source completeness remains partial, Thapar's external shortlink was skipped, and a second full hourly interval was not observed. |
| TC-09 | Separate QA DB plus live source DB. | Compared user and notice records after submission; retrieved candidates; checked institution-specific feed; validated API-reported Thapar count against the running DB's latest scrape row. | Student records stay user-scoped; official records can be retrieved; changes survive refresh. | **PASS for exercised paths.** Saved result references its candidate and submission; other-user access is denied. The running database's Thapar row (institution id 10) has 8 current official records and latest poll unchanged 8. Admin manual edits are not covered because admin content CRUD is absent. |
| TC-10 | TestClient and fault-injection test hooks. | Sent malformed registrations and invalid files/text; simulated registration commit failure; exercised no-candidate and OCR-unavailable behavior; reviewed scrape timeout/error persistence tests. | Errors are bounded, sanitized, and do not yield false verification; transient scraper failures are recorded. | **PARTIAL.** Tested bad inputs return 4xx; injected DB commit failure returns sanitized 503; missing candidate or unreadable official document remains unresolved; scrape errors persist. Concurrent upload races, active-request interruption, and an induced outage against the running DB were not exercised. |

### Focused security checks performed

- Student requests to admin-only APIs are rejected; a provisioned admin can access them. Public registration cannot self-assign admin, reviewer, or issuer roles.
- Issuer publishing requires an issuer role and active institution membership; a student publish attempt is rejected. Membership tests cover scoping and revocation.
- Student notice/candidate/result access is ownership-scoped. Cross-campus feed requests are forbidden, and students cannot choose another campus for submission.
- PDF/image ingestion has a bounded size and supported-type validation. Responses tested for malformed uploads and registration DB failure do not expose raw database driver details.
- This focused suite does not constitute a penetration test. Tokens, transport security under production hosting, concurrent upload pressure, and all possible parser payloads were not exhaustively assessed.

## B. Bugs identified and fixes

### College switch left the student feed stale

- **Reproduction:** Open Notices as a student, then select another college in the navigation bar.
- **Expected:** The notice list follows the selected college.
- **Before fix:** The selector changed but the feed continued showing the previous college.
- **Root cause:** The notices fetch effect did not depend on the authenticated user's institution id and could accept stale responses.
- **Fix:** Added the institution id dependency, clears the selected notice on change, and ignores stale network responses.
- **Retest:** In browser QA, switching from Thapar to University of Delhi refreshed the feed from 8 to 416 current notices.

### Upload validation allowed avoidable failure paths

- **Reproduction:** Submit whitespace-only text, overlong text, a large file, an empty/corrupt PDF, or an unsupported file type.
- **Expected:** A clear client error, with bounded resource use.
- **Before fix:** Some bad documents could bubble up as server errors; upload reads were not explicitly capped at the application boundary.
- **Root cause:** Validation and extraction failures were not consistently converted to API-level client errors; upload bytes were read without a strict application size cap.
- **Fix:** Added a 20 MB bounded read, a 100,000-character text limit, whitespace validation, and a typed invalid-document error mapped to HTTP 422.
- **Retest:** Regression tests cover those cases, including a real PDF upload.

### Revised schedule could be ranked as consistent

- **Reproduction:** Compare an older schedule date with a newer official notice that explicitly revises that date while also mentioning the old date.
- **Expected:** Identify the explicit revision, or conservatively request review when evidence is insufficient.
- **Before fix:** The semantic NLI model could focus on shared text and label the pair consistent despite conflicting schedule facts.
- **Root cause:** Sentence-level NLI ranking did not by itself resolve the direction of a schedule change across the full documents.
- **Fix:** Added a conservative service-level guard: an explicit revision plus conflicting schedule facts can elevate a model “consistent” result to `superseded`; title-only language does not trigger it, and unclear conflicts remain unresolved. Model weights were not changed.
- **Retest:** Regression tests cover explicit revision and title-only cases. An end-to-end comparison using cached model weights persisted `superseded` for the revised schedule example.

### Cached model loading unnecessarily depended on online metadata

- **Reproduction:** Load the semantic model where its immutable Hugging Face snapshot is already cached but the hub cannot be reached.
- **Expected:** Use the cached snapshot.
- **Before fix:** Model loading could wait/fail on remote metadata checks despite local weights being present.
- **Fix:** Resolve the cached immutable snapshot first with local-only lookup, then fall back to the configured hub id.
- **Retest:** Direct category checks and end-to-end comparison completed using the existing cached model weights.

## C. Detailed evidence and limitations

### Scraping and current Thapar records

The running API's institution list reports **8 current notices** for **Thapar University**. Its latest scheduled poll recorded `status=ok`, `found=8`, `created=0`, `modified=0`, `unchanged=8`, and no error. The database stores this run at `2026-11-17 15:15:31` UTC. This means the current Thapar direct-link items were already saved; the poll did not need to insert duplicates.

The currently exposed Thapar set includes official PDFs with extracted text. The portal's separate shortlink listing was not included because the adapter only follows supported same-site document links. It has not been verified or added. A separate legacy Thapar institution row exists in the database; the user-facing institution registry and poll above refer to the active **Thapar University** row (id 10), not that legacy row.

The configured database scrape interval is one hour. The scheduled cycle executed and `scrape_schedule_state` recorded completion at `15:15:55` UTC with the next scheduled time at `16:15:55` UTC. The cycle status is **partial**, not fully successful: VTU attachment errors and DU Google Drive/404 extraction errors were saved in the scheduler state and individual source runs. This verifies a real scheduler execution and error logging, but not repeatability across successive hourly cycles.

The Admin → Sources manual polling control was also exercised through the browser. A disposable copy of the running database was used, with a temporary admin account provisioned only in that copy; a real poll of the configured sources completed via the UI. It displayed the summary and the run table updated. The clone recorded runs of 5 new / 20 unchanged for VTU (partial), 0 new / 20 unchanged for DU (partial), 10 new / 20 unchanged for Mumbai (ok), and 0 new / 8 unchanged for Thapar (ok). Database counts in the clone increased from 885/417/1383/8 to 890/417/1393/8 (VTU/DU/Mumbai/Thapar). The clone and temporary admin were removed after verification; the user's primary database was not changed by this manual UI test.

That scheduled cycle also saved newly discovered records outside Thapar: VTU added five historical records dated January 2021 (with extracted text), DU added one November 17 listing with no document extraction, and Mumbai added 20 records (10 with publication date November 17 and 10 dated August 14; all 20 have extracted text). The live API counts after the cycle were VTU 885, DU 417, Mumbai 1383, and Thapar 8. The VTU and DU source runs were partial and their recorded extraction errors are detailed in `scrape_run` / `scrape_schedule_state`; Mumbai and Thapar completed without errors.

### Comparison and source-document limits

The real DU Tibetan-language submission matched official notice #384 by topic. The Drive URL existed in the official record, but the backend could not retrieve its PDF without Google Drive service-account configuration. FinalSay correctly explained that the document details could not be checked and did not report the pair as confirmed or fake. This is an environment/source-access limitation, not evidence that the PDF contents were compared.

The semantic model's raw scores are ranking scores, not calibrated probabilities. The schedule-revision service guard is deliberately conservative; ambiguous conflicts still go to review. Testing demonstrates the selected controlled cases and integrations, not zero-error classification for every notice type.

### Security and workflow limits

Role-based API checks cover student denial of protected admin operations and submission ownership. Upload handling restricts accepted document types and size, and input validation errors are returned without exposing server internals in the tested paths. This focused functional check is not a full security audit.

The admin official-notice CRUD workflow is incomplete (TC-07 FAIL). Admin source configuration/review features and the separate issuer publishing endpoint should not be described as a complete admin notice-management dashboard. Full admin content editing, deletion, and its end-to-end UI workflow remain unverified/unimplemented.

Not stress-tested: concurrent uploads, interruption during active processing, induced database outage, scheduler operation over a full interval, and every supported institutional site's live refresh. Some VTU/DU documents remain inaccessible or poorly extractable; Mumbai's prior scrape status was successful, but this report did not perform a new full backfill for every institution.

## D. Final results

- **Test cases executed:** 10.
- **Passed:** 7.
- **Partial:** 2.
- **Failed:** 1 (complete Admin Official Notice Management is not implemented).
- **Blocked:** 0. External source limitations are recorded as partial where relevant.
- **Confirmed fixes:** stale college feed, bounded/validated ingestion errors, conservative schedule-revision handling, and local-first semantic model loading.
- **Remaining issues:** Admin official-notice CRUD; external Google Drive document retrieval configuration; shortlink handling; undated or unextractable documents; full-interval scheduler observation; concurrency/database-outage stress tests.
- **Readiness:** Suitable for a local prototype demonstration of student submission, semantic candidate matching, source browsing, and currently available Thapar records. Not verified as a fully operational production service or as a zero-error fact checker.
