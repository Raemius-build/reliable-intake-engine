"""FastAPI application for the Reliable Intake Engine prototype."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import os
from pathlib import Path
import sqlite3
from typing import AsyncIterator, Callable

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from .crm_adapter import CRMAdapter, SQLiteSimulatedCRMAdapter
from .database import EnquiryDatabase, ProcessingStatus
from .retry_processor import (
    CLAIM_LEASE_SECONDS,
    SAFE_FAILURE_REASON,
    MAX_ATTEMPTS,
    RetryProcessor,
    current_utc_time,
    retry_delay_seconds,
)


class EnquiryPayload(BaseModel):
    """Customer enquiry fields accepted by the intake endpoint."""

    event_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: EmailStr
    phone: str | None = None
    message: str | None = None
    source: str | None = None

    @field_validator("event_id", "name", "email", mode="before")
    @classmethod
    def trim_required_strings(cls, value: object) -> object:
        """Trim whitespace from required strings before validating them."""
        if isinstance(value, str):
            return value.strip()
        return value


def default_database_path() -> Path:
    """Return the configured database path or the persistent workspace default."""
    configured_path = os.getenv("RELIABLE_INTAKE_DB_PATH")
    if configured_path:
        return Path(configured_path).expanduser()
    return (
        Path(__file__).resolve().parent.parent
        / "data"
        / "reliable_intake_engine.sqlite3"
    )


def create_app(
    database_path: str | Path | None = None,
    crm_adapter: CRMAdapter | None = None,
    clock: Callable[[], datetime] = current_utc_time,
    enable_retry_endpoint: bool | None = None,
    enable_internal_endpoints: bool | None = None,
) -> FastAPI:
    """Create the API with configurable persistence and internal operations."""
    selected_database_path = (
        Path(database_path) if database_path is not None else default_database_path()
    )
    database = EnquiryDatabase(selected_database_path)
    adapter = (
        crm_adapter
        if crm_adapter is not None
        else SQLiteSimulatedCRMAdapter(selected_database_path)
    )
    retry_processor = RetryProcessor(database, adapter, clock)
    retry_endpoint_enabled = (
        os.getenv("RELIABLE_INTAKE_ENABLE_RETRY_ENDPOINT", "").casefold()
        in {"1", "true", "yes", "on"}
        if enable_retry_endpoint is None
        else enable_retry_endpoint
    )
    internal_endpoints_enabled = (
        os.getenv("RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS", "")
        .strip()
        .casefold()
        == "true"
        if enable_internal_endpoints is None
        else enable_internal_endpoints
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        adapter.initialize()
        yield

    application = FastAPI(
        title="Reliable Intake Engine",
        description="A prototype with persistent, idempotent customer-enquiry intake.",
        lifespan=lifespan,
    )
    application.state.enquiry_database = database
    application.state.crm_adapter = adapter
    application.state.retry_processor = retry_processor

    @application.get("/health")
    def health_check() -> dict[str, str]:
        """Report whether the service is running."""
        return {"status": "healthy"}

    @application.get("/")
    def read_root() -> dict[str, str]:
        """Describe the original service foundation."""
        return {
            "service": "reliable-intake-engine",
            "description": "A customer-enquiry-to-CRM integration engine.",
            "stage": "first-stage prototype",
        }

    @application.post(
        "/webhooks/enquiries",
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            status.HTTP_200_OK: {"description": "The identical event was already received."},
            status.HTTP_409_CONFLICT: {
                "description": "The event ID was previously used with different data."
            },
        },
    )
    def accept_enquiry(
        enquiry: EnquiryPayload,
        request: Request,
        response: Response,
    ) -> dict[str, str]:
        """Validate, durably record, and hand an enquiry to the simulated CRM."""
        database: EnquiryDatabase = request.app.state.enquiry_database
        adapter: CRMAdapter = request.app.state.crm_adapter
        result = database.record(enquiry.model_dump(mode="json"), now=clock())
        event = result.event

        if result.outcome == "accepted":
            response.status_code = status.HTTP_202_ACCEPTED
            now = clock()
            claimed_event = database.claim_initial_attempt(
                event.id,
                now,
                now + timedelta(seconds=CLAIM_LEASE_SECONDS),
            )
            if claimed_event is None:
                raise RuntimeError("The new enquiry could not be claimed for handoff.")
            try:
                adapter.create_or_update_contact(claimed_event)
            except Exception:
                # Do not store exception text: it may include customer-submitted data.
                retry_at = None
                if claimed_event.attempt_count < MAX_ATTEMPTS:
                    retry_at = clock() + timedelta(
                        seconds=retry_delay_seconds(claimed_event.attempt_count)
                    )
                processing_status = database.mark_handoff_failed(
                    event.id, SAFE_FAILURE_REASON, retry_at
                )
                if processing_status == "pending_retry":
                    message = "Enquiry accepted; a CRM retry was scheduled."
                else:
                    message = "Enquiry accepted, but CRM processing failed permanently."
            else:
                database.mark_handoff_succeeded(event.id)
                processing_status = "processed"
                message = "Enquiry accepted."
        elif result.outcome == "already_received":
            response.status_code = status.HTTP_200_OK
            processing_status = event.processing_status
            if processing_status == "processed":
                message = "This event was already received; no duplicate was created."
            elif processing_status == "pending_retry":
                message = (
                    "This event was already received; a CRM retry is scheduled, "
                    "and this repeat did not trigger it."
                )
            elif processing_status == "failed":
                message = (
                    "This event was already received; CRM retries are exhausted."
                )
            else:
                message = (
                    "This event was already received; CRM processing has not completed "
                    "and was not retried."
                )
        else:
            response.status_code = status.HTTP_409_CONFLICT
            processing_status = event.processing_status
            message = "This event ID was already received with different enquiry data."

        return {
            "status": result.outcome,
            "event_id": enquiry.event_id,
            "message": message,
            "processing_status": processing_status,
        }

    if retry_endpoint_enabled:

        @application.post("/internal/retries/process-due")
        def process_due_retries(request: Request) -> dict[str, int]:
            """Manually process due retries in opted-in development environments."""
            processor: RetryProcessor = request.app.state.retry_processor
            return processor.process_due()

    if internal_endpoints_enabled:

        @application.get("/internal/events")
        def list_internal_events(
            request: Request,
            limit: int = Query(default=20, ge=1, le=100),
            offset: int = Query(default=0, ge=0),
            processing_status: ProcessingStatus | None = None,
        ) -> dict[str, object]:
            """List redacted event summaries for trusted operators."""
            database: EnquiryDatabase = request.app.state.enquiry_database
            try:
                total, events = database.list_operational_events(
                    limit, offset, processing_status
                )
            except sqlite3.Error:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Operational event data is unavailable.",
                ) from None
            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "events": events,
            }

        @application.get("/internal/events/{event_id}")
        def get_internal_event(
            event_id: str,
            request: Request,
        ) -> dict[str, object]:
            """Return redacted operational details for one event."""
            database: EnquiryDatabase = request.app.state.enquiry_database
            try:
                event = database.get_operational_event(event_id)
            except sqlite3.Error:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Operational event data is unavailable.",
                ) from None
            if event is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Event not found.",
                )
            return event

        @application.get("/internal/summary")
        def get_internal_summary(request: Request) -> dict[str, object]:
            """Return aggregate event counts for trusted operators."""
            database: EnquiryDatabase = request.app.state.enquiry_database
            try:
                return database.get_operational_summary()
            except sqlite3.Error:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Operational summary is unavailable.",
                ) from None

    return application


app = create_app()
