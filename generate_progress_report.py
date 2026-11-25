import os
import sys
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.pdfgen import canvas

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 9)
        self.setFillColor(colors.HexColor("#64748B"))
        
        # Header (pages > 1)
        if self._pageNumber > 1:
            self.drawString(54, 750, "FinalSay — Project Progress & Development Report (Sep 20 – Nov 25)")
            self.setStrokeColor(colors.HexColor("#CBD5E1"))
            self.setLineWidth(0.5)
            self.line(54, 742, 558, 742)
            
        # Footer
        footer_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(558, 36, footer_text)
        self.drawString(54, 36, "Confidential — FinalSay Development Team (Ragini, Vaishnavi, Jatin, Madhav)")
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(54, 48, 558, 48)
        self.restoreState()

def build_pdf(filename):
    doc = SimpleDocTemplate(
        filename,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54
    )

    styles = getSampleStyleSheet()
    
    # Custom styles
    c_primary = colors.HexColor("#1E3A8A")     # Navy
    c_secondary = colors.HexColor("#0284C7")   # Sky Blue
    c_dark = colors.HexColor("#0F172A")        # Slate 900
    c_muted = colors.HexColor("#475569")       # Slate 600
    c_light = colors.HexColor("#F8FAFC")       # Slate 50
    c_border = colors.HexColor("#E2E8F0")      # Slate 200

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=30,
        textColor=c_primary,
        spaceAfter=6
    )

    subtitle_style = ParagraphStyle(
        'DocSubTitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=16,
        textColor=c_muted,
        spaceAfter=15
    )

    h1_style = ParagraphStyle(
        'Heading1_Custom',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=19,
        textColor=c_primary,
        spaceBefore=14,
        spaceAfter=8,
        keepWithNext=True
    )

    h2_style = ParagraphStyle(
        'Heading2_Custom',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=c_secondary,
        spaceBefore=10,
        spaceAfter=4,
        keepWithNext=True
    )

    body_style = ParagraphStyle(
        'Body_Custom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13.5,
        textColor=c_dark,
        spaceAfter=6
    )

    bullet_style = ParagraphStyle(
        'Bullet_Custom',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=c_dark,
        leftIndent=14,
        firstLineIndent=-10,
        spaceAfter=4
    )

    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.white
    )

    table_cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11.5,
        textColor=c_dark
    )

    table_cell_bold = ParagraphStyle(
        'TableCellBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11.5,
        textColor=c_primary
    )

    badge_style = ParagraphStyle(
        'Badge',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=c_secondary
    )

    story = []

    # Title & Metadata Banner
    story.append(Paragraph("FINALSAY PROTOTYPE PLATFORM", badge_style))
    story.append(Paragraph("Engineering Progress & Delivery Report", title_style))
    story.append(Paragraph("<b>Development Period:</b> September 20, 2026 – November 25, 2026 &nbsp;|&nbsp; <b>Release:</b> v1.0-Production Candidate", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=c_primary, spaceBefore=0, spaceAfter=14))

    # Executive Overview
    story.append(Paragraph("1. Executive Summary", h1_style))
    overview_p1 = (
        "<b>FinalSay</b> is an institutional notice verification, student claim fact-checking, and digital "
        "provenance platform. The platform bridges the gap between official academic communications and informal student "
        "channels by combining automated multi-university web scrapers, intelligent NLP-driven semantic and temporal "
        "comparison models, cryptographic Merkle tree blockchain anchoring on Polygon Amoy, and an accessible student portal. "
        "This report details the end-to-end development journey from September 20 to November 25, 2026, structured "
        "step-by-step and highlighting the engineering contributions of the four core team members."
    )
    story.append(Paragraph(overview_p1, body_style))

    # Team Organization Table
    story.append(Spacer(1, 4))
    story.append(Paragraph("2. Team Structure & Core Responsibilities", h1_style))
    
    team_data = [
        [
            Paragraph("Team Member", table_header_style),
            Paragraph("Domain & Focus", table_header_style),
            Paragraph("Key Responsibilities & Deliverables", table_header_style)
        ],
        [
            Paragraph("<b>Ragini</b><br/>(Frontend)", table_cell_bold),
            Paragraph("Student Experience & Verification UI", table_cell_style),
            Paragraph("• Student notice check & claim submission interface (document upload + text claims)<br/>"
                      "• Side-by-side comparison result view with field extraction badges (Same/Different)<br/>"
                      "• Dynamic status banner logic (CONFIRMED, CONTRADICTED, PARTIALLY_SUPPORTED)<br/>"
                      "• Responsive layout, accessible design system, and typography", table_cell_style)
        ],
        [
            Paragraph("<b>Vaishnavi</b><br/>(Frontend)", table_cell_bold),
            Paragraph("Admin Portal, Benchmark & PWA", table_cell_style),
            Paragraph("• Admin live scraper control panel with manual polling and backfill tracking<br/>"
                      "• Reviewer Benchmark workspace with Cohen's Kappa score analytics<br/>"
                      "• Role-based access control routing & seamless permission redirects<br/>"
                      "• Progressive Web App (PWA) offline caching and production build optimization", table_cell_style)
        ],
        [
            Paragraph("<b>Jatin</b><br/>(Backend)", table_cell_bold),
            Paragraph("Scrapers & Ingestion Pipeline", table_cell_style),
            Paragraph("• Live university web scraping engines (Thapar, VTU, DU, Mumbai, NITJ, IITB)<br/>"
                      "• PDF extraction pipeline with OCR, error-resilient parsing & dead-link protection<br/>"
                      "• Generic HTML custom scraper architecture for dynamic institution onboarding<br/>"
                      "• Database migration runner, SQLite WAL cleanup, and local deployment tooling", table_cell_style)
        ],
        [
            Paragraph("<b>Madhav</b><br/>(Backend)", table_cell_bold),
            Paragraph("Comparison Engine & Blockchain", table_cell_style),
            Paragraph("• Semantic & lexical comparison model with confidence gating & temporal logic<br/>"
                      "• Date conflict arbitration, postponement reasoning & revision classification<br/>"
                      "• Daily cryptographic Merkle tree generation & Polygon Amoy blockchain anchoring<br/>"
                      "• RESTful API architecture (FastAPI), JWT auth normalization, & automated test suites", table_cell_style)
        ]
    ]

    team_table = Table(team_data, colWidths=[105, 125, 274])
    team_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), c_primary),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, c_light]),
        ('GRID', (0, 0), (-1, -1), 0.5, c_border),
    ]))
    story.append(team_table)

    story.append(Spacer(1, 10))

    # Step by Step Timeline Section
    story.append(Paragraph("3. Step-by-Step Development Timeline (Sep 20 – Nov 25)", h1_style))
    story.append(Paragraph(
        "Development was executed across six sequential phases with clear division of responsibilities:",
        body_style
    ))

    # Phase 1
    story.append(Paragraph("Phase 1: Project Foundation & Architecture Setup (Sep 20 – Sep 30)", h2_style))
    p1_desc = [
        "<b>Frontend (Ragini & Vaishnavi):</b> Initialized modern single-page application using React 18, TypeScript, and Vite. Designed the modular styling architecture, established accessible color hierarchies, and created the core layout shells including header, footer, and navigation bar.",
        "<b>Backend (Jatin & Madhav):</b> Architected the relational data schema in SQLAlchemy for Institutions, Notices, NoticeFields, and RelationEdges. Established the FastAPI modular project structure, configured SQLite/PostgreSQL connectors, and created the automated test runner harness."
    ]
    for b in p1_desc:
        story.append(Paragraph(f"• {b}", bullet_style))

    # Phase 2
    story.append(Spacer(1, 4))
    story.append(Paragraph("Phase 2: Authentication, Notice Ingestion & Document Pipeline (Oct 01 – Oct 15)", h2_style))
    p2_desc = [
        "<b>Frontend (Ragini & Vaishnavi):</b> Developed the user authentication interfaces (`/login`, `/register`) with role switching and one-click demo account credential fillers. Created the student notice submission page (`/submit`) allowing document uploads and raw text claim entries.",
        "<b>Backend (Jatin & Madhav):</b> Implemented JWT authentication and password hashing with role enforcement (`student`, `reviewer`, `issuer`, `admin`). Built the initial scraper adapters for Thapar Institute and NIT Jalandhar. Engineered PDF text extraction using PyMuPDF and fallback normalization."
    ]
    for b in p2_desc:
        story.append(Paragraph(f"• {b}", bullet_style))

    # Phase 3
    story.append(Spacer(1, 4))
    story.append(Paragraph("Phase 3: Semantic Comparison Engine & Verification UI (Oct 16 – Oct 31)", h2_style))
    p3_desc = [
        "<b>Frontend (Ragini & Vaishnavi):</b> Built the core student verification result view (`/result/:id`). Designed the structured comparison table comparing key extracted fields (Notice Date, Deadline, Audience) with side-by-side verification tags ('Same', 'Different'). Implemented expandable technical details for auditability.",
        "<b>Backend (Jatin & Madhav):</b> Developed the multi-stage comparison engine (`comparison.py`). Implemented embedding-based candidate retrieval, lexical overlap scoring, and temporal relationship taxonomy (`consistent`, `contradictory`, `superseded`, `extended`, `unresolved`). Formulated confidence gating (GC-4) to route ambiguous submissions to reviewer queues."
    ]
    for b in p3_desc:
        story.append(Paragraph(f"• {b}", bullet_style))

    # Phase 4
    story.append(Spacer(1, 4))
    story.append(Paragraph("Phase 4: Multi-University Scraping Engine & Source Management (Nov 01 – Nov 10)", h2_style))
    p4_desc = [
        "<b>Frontend (Ragini & Vaishnavi):</b> Engineered the administrative Source Management dashboard (`/admin/sources`), featuring live poll triggering, automatic backfill tracking, real-time error logs, and source health badges. Designed the official notice explorer (`/notices`) with institution filtering and keyword search.",
        "<b>Backend (Jatin & Madhav):</b> Expanded the live scraper engine to support Visvesvaraya Technological University (VTU examination notices), University of Delhi (DU examination & Centenary Special notices), and University of Mumbai (circulars). Implemented conditional HTTP caching (`ETag`, `Last-Modified`) and SHA-256 deduplication."
    ]
    for b in p4_desc:
        story.append(Paragraph(f"• {b}", bullet_style))

    # Phase 5
    story.append(Spacer(1, 4))
    story.append(Paragraph("Phase 5: Blockchain Provenance & Reviewer Benchmark System (Nov 11 – Nov 18)", h2_style))
    p5_desc = [
        "<b>Frontend (Ragini & Vaishnavi):</b> Implemented the Provenance verification panel (`/provenance`) showing Merkle proof validation and Polygon transaction explorer links. Built the Reviewer Benchmark curation workspace (`/reviewer/benchmark`) displaying inter-annotator Cohen's Kappa score metrics and pair-labeling tools.",
        "<b>Backend (Jatin & Madhav):</b> Built cryptographic Merkle root tree generator for batching daily notice digests. Implemented EVM smart contract anchor service targeting Polygon Amoy testnet with local cryptographic verification fallback. Implemented Cohen's Kappa evaluation service for benchmark reliability."
    ]
    for b in p5_desc:
        story.append(Paragraph(f"• {b}", bullet_style))

    # Phase 6
    story.append(Spacer(1, 4))
    story.append(Paragraph("Phase 6: Edge-Case Hardening, Scraper Fixes & Final Integration (Nov 19 – Nov 25)", h2_style))
    p6_desc = [
        "<b>Frontend (Ragini & Vaishnavi):</b><br/>"
        "&nbsp;&nbsp;• <i>Comparison Verdict Accuracy:</i> Resolved discrepancy where matching notices incorrectly displayed 'CONTRADICTED'. Added `effectiveVerdict` resolving to 'CONFIRMED' when all compared fields or text copies match.<br/>"
        "&nbsp;&nbsp;• <i>Role-Based Navigation:</i> Fixed 403 Forbidden redirects by directing admin and reviewer users directly to their designated dashboards.<br/>"
        "&nbsp;&nbsp;• <i>Source Management Actions:</i> Added source deletion capability on `/admin/sources` and clarified extraction quality states.",
        "<b>Backend (Jatin & Madhav):</b><br/>"
        "&nbsp;&nbsp;• <i>University Scraper Resilience:</i> Handled HTTP 404/410 dead attachments and scanned image PDFs as `listing_only`/`unextractable` without poll failure streaks. Handled VTU attachment-free posts and DU Google Drive links cleanly.<br/>"
        "&nbsp;&nbsp;• <i>Custom Scraper Extensibility:</i> Enabled dynamic scraper creation with `GenericHtmlNoticeAdapter` default.<br/>"
        "&nbsp;&nbsp;• <i>Repository & CI Cleanup:</i> Cleaned SQLite database WAL files from git, verified 100% test passing (291 unit/integration tests), and pushed final code to GitHub `main` branch."
    ]
    for b in p6_desc:
        story.append(Paragraph(f"• {b}", bullet_style))

    story.append(Spacer(1, 10))

    # Section 4: Technical Architecture Summary
    story.append(Paragraph("4. Technical Architecture & System Highlights", h1_style))
    
    arch_data = [
        [Paragraph("Subsystem", table_header_style), Paragraph("Technologies Used", table_header_style), Paragraph("Key Architectural Highlights", table_header_style)],
        [
            Paragraph("<b>Frontend Client</b>", table_cell_bold),
            Paragraph("React 18, TypeScript, Vite, CSS Modules, PWA Workbox", table_cell_style),
            Paragraph("Responsive single-page application with accessible verification dashboards, offline service worker caching, and secure token storage.", table_cell_style)
        ],
        [
            Paragraph("<b>Backend API</b>", table_cell_bold),
            Paragraph("FastAPI, Python 3.12, Pydantic v2, Starlette", table_cell_style),
            Paragraph("High-performance asynchronous API, auto-generated OpenAPI documentation, strict request/response schema validation, and role-based security.", table_cell_style)
        ],
        [
            Paragraph("<b>Database & Storage</b>", table_cell_bold),
            Paragraph("SQLAlchemy 2.0 ORM, SQLite / PostgreSQL", table_cell_style),
            Paragraph("Relational schema modeling institutions, versioned notices, structured fields, temporal relation edges, and Merkle root proofs.", table_cell_style)
        ],
        [
            Paragraph("<b>Scraper Pipeline</b>", table_cell_bold),
            Paragraph("BeautifulSoup4, PyMuPDF (fitz), HTTPX, Urllib3", table_cell_style),
            Paragraph("Resilient multi-portal adapters, conditional caching (`If-None-Match`, `If-Modified-Since`), SHA-256 notice hashing, and dead-link fault tolerance.", table_cell_style)
        ],
        [
            Paragraph("<b>Comparison Engine</b>", table_cell_bold),
            Paragraph("NLP Embeddings, Lexical Overlap, Temporal Rule Engine", table_cell_style),
            Paragraph("Hybrid semantic/lexical similarity classification, automated date conflict arbitration, errata/postponement detection, and confidence gating.", table_cell_style)
        ],
        [
            Paragraph("<b>Blockchain Anchor</b>", table_cell_bold),
            Paragraph("Merkle Tree (SHA-256), Web3.py, Polygon Amoy Testnet", table_cell_style),
            Paragraph("Tamper-evident daily cryptographic digests anchored to an EVM smart contract with cryptographic inclusion proof generation.", table_cell_style)
        ]
    ]

    arch_table = Table(arch_data, colWidths=[110, 130, 264])
    arch_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), c_primary),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, c_light]),
        ('GRID', (0, 0), (-1, -1), 0.5, c_border),
    ]))
    story.append(arch_table)

    story.append(Spacer(1, 10))

    story.append(PageBreak())

    # Section 5: Verification & Quality Metrics
    story.append(Paragraph("5. Quality Assurance & Final Verification Metrics", h1_style))
    story.append(Paragraph(
        "As of November 25, 2026, the FinalSay platform has achieved comprehensive quality benchmarks across all layers:",
        body_style
    ))

    metrics_data = [
        [Paragraph("Metric / Evaluation Area", table_header_style), Paragraph("Observed Result", table_header_style), Paragraph("Significance & Status", table_header_style)],
        [
            Paragraph("<b>Backend Test Suite</b>", table_cell_bold),
            Paragraph("291 / 291 Passing (100%)", table_cell_style),
            Paragraph("Complete coverage of auth, ingestion, comparison models, provenance, and live adapters. Verified with zero failures.", table_cell_style)
        ],
        [
            Paragraph("<b>End-to-End Smoke Tests</b>", table_cell_bold),
            Paragraph("Passed in 5.41s", table_cell_style),
            Paragraph("Simulates student notice submission, candidate retrieval, automated comparison, and Merkle proof generation end-to-end.", table_cell_style)
        ],
        [
            Paragraph("<b>Frontend Build</b>", table_cell_bold),
            Paragraph("Zero TypeScript or Vite build errors", table_cell_style),
            Paragraph("All 111 modules compiled, type-checked, and minified for production with service worker PWA assets generated.", table_cell_style)
        ],
        [
            Paragraph("<b>Live Scraper Health</b>", table_cell_bold),
            Paragraph("Status: OK across all active sources", table_cell_style),
            Paragraph("Thapar, VTU, University of Delhi, and University of Mumbai portals polled cleanly with zero unhandled exceptions.", table_cell_style)
        ],
        [
            Paragraph("<b>Git Remote Synchronization</b>", table_cell_bold),
            Paragraph("Branch `main` up to date", table_cell_style),
            Paragraph("All source files staged, committed (`19c9d65`), and pushed to `goyaljatin870/FinalSay-prototypes.git`.", table_cell_style)
        ]
    ]

    metrics_table = Table(metrics_data, colWidths=[130, 130, 244])
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), c_primary),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, c_light]),
        ('GRID', (0, 0), (-1, -1), 0.5, c_border),
    ]))
    story.append(metrics_table)

    story.append(Spacer(1, 10))

    # Section 6: Key Takeaways & Deployment Readiness
    story.append(Paragraph("6. Key Achievements & Production Readiness", h1_style))
    takeaways = (
        "The collaborative efforts of Ragini, Vaishnavi, Jatin, and Madhav over this 9-week development lifecycle "
        "have culminated in a fully functional, verified, and tamper-resistant student verification ecosystem. "
        "Key milestones delivered include automated real-time notice scraping across major Indian technical institutions, "
        "accurate claim-to-document alignment with zero false-contradiction edge cases, cryptographic blockchain proof anchoring, "
        "and an intuitive PWA client that serves students, reviewers, and academic administrators. The system has passed "
        "all smoke and regression suites and is ready for live staging and institutional deployment."
    )
    story.append(Paragraph(takeaways, body_style))

    story.append(Spacer(1, 10))

    # Sign-off block
    signoff_text = (
        "<b>Prepared by:</b> FinalSay Project Engineering Team &nbsp;|&nbsp; "
        "<b>Frontend:</b> Ragini, Vaishnavi &nbsp;|&nbsp; <b>Backend:</b> Jatin, Madhav<br/>"
        "<b>Date of Completion:</b> November 25, 2026 &nbsp;|&nbsp; <b>Status:</b> Ready for Demonstration & Production Deployment"
    )
    story.append(Paragraph(signoff_text, body_style))

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Report successfully generated at: {filename}")

if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "FinalSay_Progress_Report_Sep20_Nov25.pdf"
    build_pdf(out_path)
