"""FastAPI application factory: logging, middleware, routers, table creation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Event, Thread
import time

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from finalsay.api import auth as auth_api
from finalsay.api import alerts as alerts_api
from finalsay.api import admin_notices as admin_notices_api
from finalsay.api import benchmark_admin as benchmark_admin_api
from finalsay.api import comparison as comparison_api
from finalsay.api import ingestion as ingestion_api
from finalsay.api import institutions as institutions_api
from finalsay.api import issuer as issuer_api
from finalsay.api import issuer_memberships as issuer_memberships_api
from finalsay.api import notices as notices_api
from finalsay.api import provenance as provenance_api
from finalsay.api import reviewer as reviewer_api
from finalsay.config import get_settings
from finalsay.db import Base, engine, get_db
from finalsay.logging_conf import configure_logging
from finalsay.migrations import run_migrations
from finalsay.schemas import HealthResponse

# Import models so their tables register on Base.metadata before create_all.
from finalsay import models  # noqa: F401


def create_app() -> FastAPI:
    logger = configure_logging()
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        interval = settings.scrape_interval_seconds
        stop_event = Event()
        worker: Thread | None = None
        if interval > 0:
            worker = Thread(
                target=scheduled_scrape_loop,
                args=(stop_event, interval, logger),
                name="finalsay-notice-scraper",
                daemon=True,
            )
            worker.start()
            logger.info("Scheduled notice polling enabled (every %d seconds)", interval)
        else:
            logger.info("Scheduled notice polling disabled")
        try:
            yield
        finally:
            stop_event.set()
            if worker is not None:
                worker.join(timeout=2)

    from finalsay.services.scrape_runtime import scheduled_scrape_loop

    app = FastAPI(title="FinalSay", version="0.2.0", lifespan=lifespan)

    # CORS for split deployment (e.g. GitHub Pages frontend talking to an EC2
    # backend). Auth rides in the Authorization header, not cookies, so
    # credentialed requests are never needed — which is what lets a deployment
    # keep the wildcard default without the browser rejecting the response.
    origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Create tables on startup (fine for SQLite demo; Postgres uses same models).
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)

    from finalsay.db import SessionLocal
    from finalsay.auth import hash_password
    from finalsay.models import User, Institution
    with SessionLocal() as db:
        if settings.initial_admin_email and settings.initial_admin_password:
            from finalsay.services.provision_admin import create_first_admin
            try:
                create_first_admin(
                    db,
                    settings.initial_admin_email,
                    settings.initial_admin_password,
                    display_name="Administrator",
                )
                db.commit()
                logger.info("Provisioned initial administrator %s", settings.initial_admin_email)
            except ValueError:
                db.rollback()

        # Provision fallback demo accounts so logins are always available out of the box
        admin_email = "admin@finalsay.local"
        existing_admin = db.scalar(select(User).where(User.email == admin_email))
        if existing_admin is None:
            admins_count = db.scalar(select(func.count(User.id)).where(User.role == "admin")) or 0
            if admins_count == 0:
                demo_admin = User(
                    email=admin_email,
                    hashed_password=hash_password("AdminPass12345678!"),
                    role="admin",
                    display_name="Administrator",
                )
                db.add(demo_admin)
                db.commit()
                logger.info("Provisioned demo admin %s", admin_email)

        student_email = "student@finalsay.local"
        existing_student = db.scalar(select(User).where(User.email == student_email))
        if existing_student is None:
            first_inst = db.scalar(select(Institution).where(Institution.active.is_(True)).order_by(Institution.id))
            demo_student = User(
                email=student_email,
                hashed_password=hash_password("Student123!"),
                role="student",
                display_name="Sample Student",
                institution_id=first_inst.id if first_inst else None,
            )
            db.add(demo_student)
            db.commit()
            logger.info("Provisioned demo student %s (institution: %s)", student_email, first_inst.name if first_inst else None)

    @app.middleware("http")
    async def request_logging_middleware(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s -> %s (%.1fms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error processing %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Server processing error: {str(exc)}"},
        )

    @app.get("/api/health", response_model=HealthResponse, tags=["health"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/api/health/ready", response_model=HealthResponse, tags=["health"])
    def health_ready(db: Session = Depends(get_db)) -> HealthResponse:
        """Readiness probe: fails while the database is unreachable."""
        db.execute(text("SELECT 1"))
        return HealthResponse(status="ok")

    app.include_router(auth_api.router)
    app.include_router(alerts_api.router)
    app.include_router(admin_notices_api.router)
    app.include_router(benchmark_admin_api.router)
    app.include_router(provenance_api.router)
    app.include_router(comparison_api.router)
    app.include_router(ingestion_api.router)
    app.include_router(institutions_api.router)
    app.include_router(notices_api.router)
    app.include_router(reviewer_api.router)
    app.include_router(issuer_api.router)
    app.include_router(issuer_memberships_api.router)
    return app


app = create_app()

