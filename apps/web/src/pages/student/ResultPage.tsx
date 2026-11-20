import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, errorMessage } from '../../api/client'
import type {
  CandidateOut,
  ComparisonResponse,
  NoticeDetail,
  VerifyResponse,
} from '../../api/types'
import { loadResult, saveResult } from '../../api/resultCache'
import { useNoticeFollows } from '../../hooks/useNoticeFollows'
import { useAuth } from '../../auth/AuthContext'

// Keep the first screen readable for students; evidence and machine details
// remain available in collapsed sections for reviewers and technical users.
export default function ResultPage() {
  const { id } = useParams<{ id: string }>()
  const { user } = useAuth()
  const submissionId = Number(id)

  const [submission, setSubmission] = useState<NoticeDetail | null>(null)
  const [matchedNoticeDetail, setMatchedNoticeDetail] = useState<NoticeDetail | null>(null)
  const [candidates, setCandidates] = useState<CandidateOut[]>([])
  const [verify, setVerify] = useState<VerifyResponse | null>(null)
  const [outcome, setOutcome] = useState<ComparisonResponse | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [verifying, setVerifying] = useState(false)
  const [rechecking, setRechecking] = useState(false)
  const { noticeIds, busyIds, toggle: toggleFollow } = useNoticeFollows(user?.role === 'student')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    const cachedOutcome = loadResult(submissionId)
    setOutcome(cachedOutcome)
    setMatchedNoticeDetail(null)
    try {
      const [sub, cands] = await Promise.all([
        api.get<NoticeDetail>(`/notices/${submissionId}`),
        api.get<CandidateOut[]>(`/notices/${submissionId}/candidates`),
      ])
      setSubmission(sub.data)
      setCandidates(cands.data)
      let currentOutcome = cachedOutcome
      try {
        const persisted = await api.get<ComparisonResponse>(
          `/ingest/submissions/${submissionId}/result`,
        )
        currentOutcome = persisted.data
        setOutcome(currentOutcome)
        saveResult(currentOutcome)
      } catch (err) {
        // Keep the immediate local result visible when the persisted-result
        // endpoint is temporarily unavailable or an older backend is running.
        if (!cachedOutcome) setError(errorMessage(err))
      }
      if (currentOutcome?.candidate_id) {
        try {
          const official = await api.get<NoticeDetail>(`/notices/${currentOutcome.candidate_id}`)
          setMatchedNoticeDetail(official.data)
        } catch {
          // Candidate title and source still render from the ranked result.
        }
      }
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [submissionId])

  useEffect(() => {
    void load()
  }, [load, submissionId])

  const runVerify = async (tamper: boolean) => {
    const officialNoticeId = outcome?.candidate_id ?? candidates[0]?.notice.id
    if (!officialNoticeId) {
      setError('No matched official notice is available to verify.')
      return
    }
    setVerifying(true)
    setError('')
    try {
      const res = await api.get<VerifyResponse>(
        `/provenance/verify/${officialNoticeId}`,
        { params: { tamper } },
      )
      setVerify(res.data)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setVerifying(false)
    }
  }

  const recheckSubmission = async () => {
    setRechecking(true)
    setError('')
    try {
      const refreshed = await api.post<ComparisonResponse>(`/compare/${submissionId}`)
      setOutcome(refreshed.data)
      saveResult(refreshed.data)
      const [ranked, official] = await Promise.all([
        api.get<CandidateOut[]>(`/notices/${submissionId}/candidates`),
        refreshed.data.candidate_id
          ? api.get<NoticeDetail>(`/notices/${refreshed.data.candidate_id}`)
          : Promise.resolve(null),
      ])
      setCandidates(ranked.data)
      setMatchedNoticeDetail(official?.data ?? null)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setRechecking(false)
    }
  }

  if (loading) return <p className="muted">Loading result…</p>
  if (error && !submission) return <p className="error">{error}</p>
  if (!submission) return <p className="muted">No submission found.</p>

  const topCandidate = candidates[0]
  const matchedCandidate = candidates.find(
    (item) => item.notice.id === outcome?.candidate_id,
  )?.notice ?? (
    outcome?.candidate_id && matchedNoticeDetail?.id === outcome.candidate_id
      ? matchedNoticeDetail
      : outcome?.candidate_id ? undefined : topCandidate?.notice
  )
  const hasExtractedSourceDocument = Boolean(
    outcome?.candidate_supplemental_extractions?.some(
      (item) => item.extraction_quality === 'extracted' && item.extracted_text.trim(),
    ),
  )
  const listingOnlyMatch = Boolean(
    matchedCandidate &&
      !hasExtractedSourceDocument &&
      (['listing_only', 'unextractable'].includes(matchedCandidate.extraction_quality) ||
        (matchedCandidate.extraction_quality === 'unknown' &&
          /official listing\s*:/i.test(outcome?.candidate_text ?? ''))),
  )
  const integrityMessage = (status: VerifyResponse['integrity_status']) => {
    switch (status) {
      case 'VERIFIED': return 'Public Polygon Amoy anchor and notice integrity verified.'
      case 'LOCAL_VERIFIED': return 'Local hash and Merkle proof match. This notice is not anchored to a public chain.'
      case 'TAMPERED': return 'Tampering detected: the notice hash or Merkle proof does not match.'
      case 'PENDING': return 'The anchor transaction is pending confirmation.'
      case 'VERIFICATION_UNAVAILABLE': return 'The public chain could not be reached; verification is unavailable.'
      default: return 'No confirmed public blockchain anchor exists for this notice.'
    }
  }
  const detailText = (key: string) => {
    const value = verify?.details[key]
    return typeof value === 'string' ? value : null
  }
  const normalizedSubmissionText = (submission.redacted_text || '').replace(/\s+/g, ' ').trim()
  const normalizedOfficialText = (matchedNoticeDetail?.redacted_text || '').replace(/\s+/g, ' ').trim()
  const exactTextMatch = Boolean(
    normalizedSubmissionText && normalizedOfficialText && normalizedSubmissionText === normalizedOfficialText,
  )
  const isTypedClaim = Boolean(
    (submission.submission_mode === 'claim' || (submission.submission_mode === 'legacy' && !submission.title &&
      (submission.redacted_text || '').length <= 900 &&
      !/\b(?:notice|notification|circular|issued by|university|registrar|subject:)\b/i.test(submission.redacted_text || ''))),
  )

  const resultCopy = (() => {
    if (outcome?.student_verdict) {
      const copy = {
        CONFIRMED: { heading: 'This information matches the official notice', next: 'Open the official notice to check the full instructions.' },
        CONTRADICTED: { heading: 'This detail conflicts with the official notice', next: 'Follow the official notice. Contact your college if you need clarification.' },
        PARTIALLY_SUPPORTED: { heading: 'Some details are supported; others are unclear', next: 'Check the official notice and confirm the unclear detail with your college.' },
        NOT_FOUND: { heading: 'No matching official notice was found', next: 'Check again later or ask your college office to confirm.' },
        NEEDS_MANUAL_VERIFICATION: { heading: 'A possible notice needs a closer check', next: 'Open the source or ask your college office to confirm.' },
      }[outcome.student_verdict]
      const documentBody = listingOnlyMatch
        ? 'A related portal listing was found, but FinalSay could not read the official document. Its details need manual checking.'
        : outcome.label === 'consistent'
          ? exactTextMatch
            ? 'The text in your copy is the same as the readable official notice.'
            : 'No important difference was found in the readable official notice.'
          : outcome.label === 'superseded'
            ? 'The institution published a newer notice. Follow its latest instructions.'
            : outcome.label === 'extended'
              ? 'The institution published a later deadline.'
              : outcome.label === 'corrected'
                ? 'The institution published a correction. Follow the corrected details.'
                : 'Review the source below before acting on this result.'
      return {
        ...copy,
        body: outcome.student_summary || documentBody,
      }
    }
    if (listingOnlyMatch) {
      return {
        heading: 'Possible match; details could not be checked',
        body: 'We matched the topic to an official university listing, but its PDF is unavailable to FinalSay. We cannot tell whether any date or other detail in your copy is wrong. This is not a finding that your notice is fake.',
        next: 'Open the official source. If the document is unavailable, ask your college office to confirm the details.',
      }
    }
    switch (outcome?.label) {
      case 'consistent':
        return exactTextMatch
          ? { heading: 'Exact text match', body: 'The text in your copy is the same as the readable official notice.', next: 'Open the official source if you want to read the original.' }
          : { heading: 'No important difference found', body: 'The comparison found this copy consistent with the readable official notice. It may not be an exact word-for-word copy.', next: 'Open the official source if you want to read the original.' }
      case 'superseded':
        return { heading: 'A newer notice replaces this one', body: 'The institution has published a newer version. Use the newer notice.', next: 'Open the official source and follow its latest instructions.' }
      case 'corrected':
        return { heading: 'The institution corrected this notice', body: 'Some details differ from the correction it published.', next: 'Open the official source and follow the corrected details.' }
      case 'extended':
        return { heading: 'The deadline was extended', body: 'The institution published a later deadline.', next: 'Open the official source and check the new deadline.' }
      case 'cancelled':
        return { heading: 'The institution cancelled this notice', body: 'A cancellation was found in the official notices.', next: 'Open the official source for any replacement instructions.' }
      case 'contradictory':
        return { heading: 'Some details do not match', body: 'We found a conflict with the institution’s notice.', next: 'Check the official source before acting on this copy.' }
      default:
        return outcome?.candidate_id
          ? { heading: 'Needs a manual check', body: 'We found a possible match, but there is not enough evidence to confirm it. This does not mean your notice is fake.', next: 'Open the official source or ask your college office to confirm.' }
          : { heading: 'No matching notice found yet', body: 'We could not find a close match in the notices currently available. This does not mean your notice is fake.', next: 'Check again later or ask your college office to confirm.' }
    }
  })()

  const submittedField = (name: string) =>
    submission.fields.find((field) => field.field_name.toLowerCase() === name)?.value?.trim() || null
  const officialField = (name: string, fallback?: string | null) =>
    matchedNoticeDetail?.fields.find((field) => field.field_name.toLowerCase() === name)?.value?.trim() || fallback?.trim() || null
  const normalizeForCompare = (value: string) => value
    .normalize('NFKC')
    .toLocaleLowerCase()
    .replace(/[.,;:()[\]{}]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  const comparedFields = [
    { key: 'date', label: 'Notice date', submitted: submittedField('date'), official: officialField('date', matchedCandidate?.notice_date) },
    { key: 'deadline', label: 'Deadline', submitted: submittedField('deadline'), official: officialField('deadline', matchedCandidate?.deadline) },
    { key: 'audience', label: 'Who it applies to', submitted: submittedField('audience'), official: officialField('audience', matchedCandidate?.audience) },
  ].filter((field) => field.submitted || field.official)
  const hasOfficialDocumentText = Boolean(
    (matchedNoticeDetail?.extraction_quality === 'extracted' && matchedNoticeDetail.redacted_text?.trim()) ||
    outcome?.candidate_supplemental_extractions?.some(
      (item) => item.extraction_quality === 'extracted' && item.extracted_text.trim(),
    ),
  )

  return (
    <div className="stack">
      <div className="card">
        <h1>Notice check</h1>
        <p className="muted result-submission-id">Submission #{submission.id}</p>
        {outcome ? (
          <div className="stack-sm result-summary">
            {outcome.student_verdict && <p className="eyebrow">{outcome.student_verdict.replace(/_/g, ' ')}</p>}
            <h2>{resultCopy.heading}</h2>
            <p>{resultCopy.body}</p>
            <button type="button" className="btn-secondary" disabled={rechecking} onClick={() => void recheckSubmission()}>
              {rechecking ? 'Checking current notices…' : 'Recheck against current notices'}
            </button>
          </div>
        ) : (
          <p className="muted">A comparison result is not available for this submission yet.</p>
        )}
        {matchedCandidate && (
          <section className="result-match">
            <p className="eyebrow">OFFICIAL NOTICE MATCH</p>
            <h2>{matchedCandidate.title || `Official notice #${matchedCandidate.id}`}</h2>
            <p className="muted">{outcome?.candidate_institution || 'Your selected institution'} · Notice #{matchedCandidate.id}{matchedCandidate.published_date ? ` · Published ${matchedCandidate.published_date}` : ''}</p>
            {outcome?.candidate_source_url && (
              <p><a href={outcome.candidate_source_url} target="_blank" rel="noreferrer">Open official source ↗</a></p>
            )}
            {user?.role === 'student' && <button
              type="button"
              className="btn-secondary"
              disabled={busyIds.has(matchedCandidate.id)}
              onClick={() => void toggleFollow(matchedCandidate.id).catch((err) => setError(errorMessage(err)))}
            >
              {busyIds.has(matchedCandidate.id) ? 'Saving…' : noticeIds.has(matchedCandidate.id) ? 'Following this notice' : 'Follow this notice'}
            </button>}
          </section>
        )}
        {isTypedClaim && submission.redacted_text?.trim() && (
          <section className="result-differences">
            <p className="eyebrow">YOUR CLAIM</p>
            <blockquote>{submission.redacted_text}</blockquote>
          </section>
        )}
        {matchedCandidate && !outcome?.student_summary && (
          <section className="result-differences">
            <h3>{submission.title ? 'What differs from your copy?' : 'What the official notice says'}</h3>
            {listingOnlyMatch ? (
              <>
                <p>FinalSay recognized the topic, but couldn’t read the official PDF. So it couldn’t verify whether dates or other details in your copy were changed.</p>
                <div className="result-fact-list">
                  <div><strong>Date detected in your copy</strong><span>{submittedField('date') || 'Not detected'}</span></div>
                  <div><strong>Official portal listing date</strong><span>{matchedCandidate.published_date || 'Not shown'}</span></div>
                  <div><strong>What that means</strong><span>The listing date is when the notice was posted; it does not confirm the dates printed in the PDF.</span></div>
                </div>
              </>
            ) : comparedFields.length > 0 ? (
              <>
                <p className="muted">These are the fields FinalSay could extract. “Different” means the extracted values differ; OCR can make mistakes.</p>
                <div className="table-scroll">
                  <table>
                    <thead><tr><th>Detail</th><th>Your copy</th><th>Official notice</th><th>Check</th></tr></thead>
                    <tbody>{comparedFields.map((field) => {
                      const same = field.submitted && field.official && normalizeForCompare(field.submitted) === normalizeForCompare(field.official)
                      const different = field.submitted && field.official && !same
                      return <tr key={field.key}>
                        <th>{field.label}</th>
                        <td>{field.submitted || 'Not found in your copy'}</td>
                        <td>{field.official || 'Not available in official record'}</td>
                        <td>{same ? 'Same' : different ? 'Different' : 'Cannot compare'}</td>
                      </tr>
                    })}</tbody>
                  </table>
                </div>
              </>
            ) : (
              <p>{hasOfficialDocumentText
                ? 'FinalSay compared the notice text and found no date or deadline fields it could reliably display side by side.'
                : 'The official record does not contain enough readable detail to show which part of your copy differs.'}</p>
            )}
          </section>
        )}
        <section className="result-next">
          <h3>Next step</h3>
          <p>{resultCopy.next}</p>
          {!matchedCandidate && <p><Link to="/notices">Browse official notices</Link></p>}
        </section>
      </div>

      <details className="card result-details">
        <summary>Why did I get this result?</summary>
        <p>{outcome?.rationale || 'No comparison explanation is available.'}</p>
        {outcome?.gated && <p className="muted">This result was sent for a closer review. It is not a finding that your notice is fake.</p>}
        {outcome?.evidence && outcome.evidence.length > 0 && <ul>{outcome.evidence.map((item, index) => <li key={index}>{item}</li>)}</ul>}
        {outcome?.model === 'claim-schedule-evidence' ? (
          <p className="muted">This verdict comes from the matched schedule row; no model score was used.</p>
        ) : outcome?.confidence != null && outcome.model !== 'claim-manual-review' ? (
          <p className="muted">Technical model score: {outcome.confidence.toFixed(3)} (not a probability).</p>
        ) : null}
        <p className="muted">Comparison model: {outcome?.model || 'not available'}</p>
      </details>

      <details className="card result-details">
        <summary>View extracted text and fields</summary>
        {submission.fields.length > 0 && (
          <table>
            <thead><tr><th>Field</th><th>Value</th><th>Confidence</th></tr></thead>
            <tbody>{submission.fields.map((field) => (
              <tr key={field.field_name}><td>{field.field_name}</td><td>{field.value ?? '—'}</td><td>{field.confidence != null ? field.confidence.toFixed(2) : '—'}</td></tr>
            ))}</tbody>
          </table>
        )}
        {submission.redacted_text && <><h3>Your notice text (redacted)</h3><pre className="pre">{submission.redacted_text}</pre></>}
        {outcome?.candidate_text && <><h3>Official listing text</h3><pre className="pre">{outcome.candidate_text}</pre></>}
        {outcome?.candidate_supplemental_extractions?.map((extraction) => (
          <section key={`${extraction.source_url}-${extraction.extracted_text_sha256}`}>
            <h3>Text extracted from the official document</h3>
            <p className="muted">{extraction.extraction_quality} · {extraction.extraction_method}</p>
            <pre className="pre">{extraction.extracted_text}</pre>
          </section>
        ))}
      </details>

      <details className="card result-details">
        <summary>Technical integrity check</summary>
        <p className="muted">Check the stored hash and Merkle proof for the matched official notice.</p>
        <div className="row">
          <button disabled={verifying || (!outcome?.candidate_id && !topCandidate)} onClick={() => runVerify(false)}>
            {verifying ? 'Checking…' : 'Check notice integrity'}
          </button>
        </div>
        {verify && (
          <div className={`banner ${verify.integrity_status === 'TAMPERED' ? 'banner-bad' : verify.integrity_status === 'VERIFIED' || verify.integrity_status === 'LOCAL_VERIFIED' ? 'banner-ok' : ''}`}>
            <strong>{integrityMessage(verify.integrity_status)}</strong>
            <p>{verify.integrity_status}</p>
            {detailText('anchor_error') && <p>{detailText('anchor_error')}</p>}
            <details><summary>Advanced verification data</summary><pre className="pre">{JSON.stringify(verify.details, null, 2)}</pre></details>
          </div>
        )}
      </details>

      {error && <p className="error">{error}</p>}
    </div>
  )
}
