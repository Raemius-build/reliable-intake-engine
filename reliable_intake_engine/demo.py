"""Run a deterministic end-to-end demonstration with synthetic enquiry data."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import TextIO

from fastapi.testclient import TestClient

from .crm_adapter import ContactUpsertResult, SQLiteSimulatedCRMAdapter
from .database import EnquiryEvent
from .main import create_app

SAFE_FAILURE_REASON = "CRM handoff failed; adapter exception details are withheld."

SUCCESSFUL_ENQUIRY = {
    "event_id": "demo-success-001",
    "name": "Demo Customer Alpha",
    "email": "alpha@example.com",
    "message": "Synthetic enquiry for the local demonstration.",
    "source": "demo",
}
RETRY_ENQUIRY = {
    "event_id": "demo-retry-001",
    "name": "Demo Customer Beta",
    "email": "beta@example.com",
    "message": "Synthetic enquiry used to demonstrate recovery.",
    "source": "demo",
}
EXHAUSTED_ENQUIRY = {
    "event_id": "demo-exhausted-001",
    "name": "Demo Customer Gamma",
    "email": "gamma@example.com",
    "message": "Synthetic enquiry used to demonstrate exhausted retries.",
    "source": "demo",
}


class DemoClock:
    """A controllable UTC clock; advancing it never waits in real time."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class DemoCRMAdapter:
    """Use the real SQLite contact adapter and inject scripted demo failures."""

    def __init__(self, database_path: Path) -> None:
        self.delegate = SQLiteSimulatedCRMAdapter(database_path)
        self.attempts_by_event: dict[str, int] = {}

    def initialize(self) -> None:
        self.delegate.initialize()

    def create_or_update_contact(self, event: EnquiryEvent) -> ContactUpsertResult:
        attempt = self.attempts_by_event.get(event.event_id, 0) + 1
        self.attempts_by_event[event.event_id] = attempt

        if event.event_id == RETRY_ENQUIRY["event_id"] and attempt == 1:
            # Model a successful CRM write whose acknowledgement was lost.
            self.delegate.create_or_update_contact(event)
            raise RuntimeError("Simulated temporary CRM failure.")

        if event.event_id == EXHAUSTED_ENQUIRY["event_id"]:
            raise RuntimeError("Simulated persistent CRM failure.")

        return self.delegate.create_or_update_contact(event)


def _check_status(response, expected_status: int, scenario: str) -> None:
    if response.status_code != expected_status:
        raise RuntimeError(f"Demo scenario failed: {scenario}.")


def _event_detail(client: TestClient, event_id: str) -> dict[str, object]:
    response = client.get(f"/internal/events/{event_id}")
    _check_status(response, 200, "event inspection")
    return response.json()


def _contact_count(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM simulated_crm_contacts"
        ).fetchone()
    return int(row[0])


def run_demo(database_path: Path, output: TextIO) -> None:
    """Run all demonstration flows against a new, caller-provided SQLite file."""
    if database_path.exists():
        raise FileExistsError("The demo requires a new, isolated SQLite file.")

    clock = DemoClock()
    adapter = DemoCRMAdapter(database_path)
    application = create_app(
        database_path,
        crm_adapter=adapter,
        clock=clock,
        enable_retry_endpoint=True,
        enable_internal_endpoints=True,
    )

    with TestClient(application) as client:
        accepted = client.post("/webhooks/enquiries", json=SUCCESSFUL_ENQUIRY)
        _check_status(accepted, 202, "successful enquiry")
        success_detail = _event_detail(client, SUCCESSFUL_ENQUIRY["event_id"])
        if success_detail["processing_status"] != "processed":
            raise RuntimeError("Demo scenario failed: successful enquiry state.")
        print(
            "1. Valid enquiry: HTTP 202; state=processed; "
            "attempts=1; contacts=1",
            file=output,
        )

        duplicate = client.post("/webhooks/enquiries", json=SUCCESSFUL_ENQUIRY)
        _check_status(duplicate, 200, "duplicate enquiry")
        list_after_duplicate = client.get("/internal/events?limit=100")
        _check_status(list_after_duplicate, 200, "duplicate event count")
        duplicate_event_count = list_after_duplicate.json()["total"]
        duplicate_contact_count = _contact_count(database_path)
        if duplicate_event_count != 1 or duplicate_contact_count != 1:
            raise RuntimeError("Demo scenario failed: duplicate was not idempotent.")
        print(
            "2. Identical repeat: HTTP 200; events=1; contacts=1",
            file=output,
        )

        temporary_failure = client.post(
            "/webhooks/enquiries", json=RETRY_ENQUIRY
        )
        _check_status(temporary_failure, 202, "temporary CRM failure")
        pending_detail = _event_detail(client, RETRY_ENQUIRY["event_id"])
        if (
            pending_detail["processing_status"] != "pending_retry"
            or pending_detail["attempt_count"] != 1
            or pending_detail["next_retry_at"] is None
            or pending_detail["last_failure_reason"] != SAFE_FAILURE_REASON
        ):
            raise RuntimeError("Demo scenario failed: retry was not persisted.")
        contacts_after_failure = _contact_count(database_path)
        print(
            "3. Temporary CRM failure: HTTP 202; state=pending_retry; "
            f"attempts=1; contacts={contacts_after_failure}",
            file=output,
        )

        clock.advance(30)
        retry_response = client.post("/internal/retries/process-due")
        _check_status(retry_response, 200, "due retry")
        recovered_detail = _event_detail(client, RETRY_ENQUIRY["event_id"])
        contacts_after_retry = _contact_count(database_path)
        if (
            retry_response.json()["processed"] != 1
            or recovered_detail["processing_status"] != "processed"
            or recovered_detail["attempt_count"] != 2
            or contacts_after_retry != contacts_after_failure
        ):
            raise RuntimeError("Demo scenario failed: retry recovery.")
        print(
            "4. Due retry (+30s simulated): HTTP 200; state=processed; "
            f"attempts=2; contacts={contacts_after_retry}",
            file=output,
        )

        initial_exhausted = client.post(
            "/webhooks/enquiries", json=EXHAUSTED_ENQUIRY
        )
        _check_status(initial_exhausted, 202, "initial exhausted-retry attempt")
        clock.advance(30)
        second_exhausted_attempt = client.post("/internal/retries/process-due")
        _check_status(second_exhausted_attempt, 200, "second exhausted-retry attempt")
        clock.advance(60)
        final_exhausted_attempt = client.post("/internal/retries/process-due")
        _check_status(final_exhausted_attempt, 200, "final exhausted-retry attempt")
        exhausted_detail = _event_detail(client, EXHAUSTED_ENQUIRY["event_id"])
        summary_response = client.get("/internal/summary")
        _check_status(summary_response, 200, "final operational summary")
        summary = summary_response.json()
        final_contact_count = _contact_count(database_path)
        if (
            exhausted_detail["processing_status"] != "failed"
            or exhausted_detail["attempt_count"] != 3
            or exhausted_detail["next_retry_at"] is not None
            or second_exhausted_attempt.json()["rescheduled"] != 1
            or final_exhausted_attempt.json()["failed"] != 1
            or summary["terminal_failures"] != 1
            or summary["total_events"] != 3
            or final_contact_count != contacts_after_retry
        ):
            raise RuntimeError("Demo scenario failed: retry exhaustion.")
        print(
            "5. Retry limit exhausted: state=failed; attempts=3; "
            f"contacts={final_contact_count}; "
            f"terminal_failures={summary['terminal_failures']}",
            file=output,
        )

    print(
        "Demo complete. The temporary SQLite file is removed when the command exits.",
        file=output,
    )


def main() -> None:
    """Run the demo in a temporary directory removed when the command exits."""
    with TemporaryDirectory(prefix="reliable-intake-demo-") as temporary_directory:
        database_path = Path(temporary_directory) / "demo.sqlite3"
        run_demo(database_path, sys.stdout)


if __name__ == "__main__":
    main()
