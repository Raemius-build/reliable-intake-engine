"""End-to-end reliability checks for complete enquiry lifecycles."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from reliable_intake_engine.crm_adapter import (
    ContactUpsertResult,
    SQLiteSimulatedCRMAdapter,
)
from reliable_intake_engine.database import EnquiryDatabase, EnquiryEvent
from reliable_intake_engine.retry_processor import RetryProcessor
from reliable_intake_engine.main import create_app

SAFE_FAILURE_REASON = "CRM handoff failed; adapter exception details are withheld."

VALID_ENQUIRY = {
    "event_id": "evt_end_to_end_001",
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "phone": "+44 7700 900123",
    "message": "Please contact me about the integration.",
    "source": "website",
}


class ControlledClock:
    """Advanceable UTC clock so retry tests do not wait in real time."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class ScriptedCRMAdapter:
    """Delegate real SQLite contact upserts and inject selected failures."""

    def __init__(
        self,
        database_path: Path,
        *,
        fail_on_calls: set[int] | None = None,
        apply_before_failure: bool = False,
    ) -> None:
        self.delegate = SQLiteSimulatedCRMAdapter(database_path)
        self.fail_on_calls = fail_on_calls or set()
        self.apply_before_failure = apply_before_failure
        self.call_count = 0

    def initialize(self) -> None:
        self.delegate.initialize()

    def create_or_update_contact(self, event: EnquiryEvent) -> ContactUpsertResult:
        self.call_count += 1
        should_fail = self.call_count in self.fail_on_calls
        if should_fail and self.apply_before_failure:
            self.delegate.create_or_update_contact(event)
        if should_fail:
            raise RuntimeError(
                f"Simulated adapter failure for {event.email}: {event.message}"
            )
        return self.delegate.create_or_update_contact(event)


def make_e2e_app(database_path: Path, adapter, clock: ControlledClock):
    return create_app(
        database_path,
        crm_adapter=adapter,
        clock=clock,
        enable_retry_endpoint=True,
        enable_internal_endpoints=True,
    )


def read_event_state(database_path: Path, event_id: str) -> tuple:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            """
            SELECT processing_status, attempt_count, next_retry_at,
                   last_failure_reason
            FROM enquiry_events
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()


def read_contacts(database_path: Path) -> list[tuple]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            """
            SELECT contact_id, normalized_email, name, last_event_id
            FROM simulated_crm_contacts
            ORDER BY contact_id
            """
        ).fetchall()


def test_successful_webhook_lifecycle_is_visible_and_redacted(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "success.sqlite3"
    clock = ControlledClock()
    adapter = SQLiteSimulatedCRMAdapter(database_path)

    with TestClient(make_e2e_app(database_path, adapter, clock)) as client:
        health = client.get("/health")
        root = client.get("/")
        accepted = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        event_list = client.get("/internal/events")
        event_detail = client.get(
            f"/internal/events/{VALID_ENQUIRY['event_id']}"
        )
        summary = client.get("/internal/summary")

    assert health.status_code == 200
    assert health.json() == {"status": "healthy"}
    assert root.status_code == 200
    assert root.json()["service"] == "reliable-intake-engine"
    assert accepted.status_code == 202
    assert accepted.json()["processing_status"] == "processed"
    assert event_list.status_code == 200
    assert event_list.json()["total"] == 1
    assert event_list.json()["events"][0]["event_id"] == VALID_ENQUIRY["event_id"]
    assert event_list.json()["events"][0]["processing_status"] == "processed"
    assert event_detail.status_code == 200
    assert event_detail.json()["attempt_count"] == 1
    assert event_detail.json()["next_retry_at"] is None
    assert event_detail.json()["last_failure_reason"] is None
    assert summary.json()["total_events"] == 1
    assert summary.json()["by_processing_status"]["processed"] == 1

    persisted = read_event_state(database_path, VALID_ENQUIRY["event_id"])
    assert persisted == ("processed", 1, None, None)
    contacts = read_contacts(database_path)
    assert len(contacts) == 1
    assert contacts[0][1:] == (
        "ada@example.com",
        "Ada Lovelace",
        VALID_ENQUIRY["event_id"],
    )
    for customer_value in (
        VALID_ENQUIRY["name"],
        VALID_ENQUIRY["email"],
        VALID_ENQUIRY["phone"],
        VALID_ENQUIRY["message"],
    ):
        assert customer_value not in event_list.text
        assert customer_value not in event_detail.text


def test_duplicate_webhook_lifecycle_keeps_one_event_and_contact(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "duplicate.sqlite3"
    clock = ControlledClock()
    adapter = SQLiteSimulatedCRMAdapter(database_path)

    with TestClient(make_e2e_app(database_path, adapter, clock)) as client:
        first = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        repeated = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        event_list = client.get("/internal/events")
        summary = client.get("/internal/summary")

    assert first.status_code == 202
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "already_received"
    assert event_list.json()["total"] == 1
    assert summary.json()["total_events"] == 1
    assert summary.json()["by_processing_status"]["processed"] == 1
    assert read_event_state(database_path, VALID_ENQUIRY["event_id"])[0:2] == (
        "processed",
        1,
    )
    assert len(read_contacts(database_path)) == 1


def test_distinct_events_share_and_update_normalized_email_contact(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "identity.sqlite3"
    clock = ControlledClock()
    adapter = SQLiteSimulatedCRMAdapter(database_path)
    second_enquiry = {
        **VALID_ENQUIRY,
        "event_id": "evt_end_to_end_002",
        "name": "Ada Byron",
        "email": " ADA@EXAMPLE.COM ",
        "message": "A later enquiry.",
    }

    with TestClient(make_e2e_app(database_path, adapter, clock)) as client:
        first = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        second = client.post("/webhooks/enquiries", json=second_enquiry)
        summary = client.get("/internal/summary")
        latest_event = client.get("/internal/events/evt_end_to_end_002")

    assert first.status_code == second.status_code == 202
    assert len(read_contacts(database_path)) == 1
    contact = read_contacts(database_path)[0]
    assert contact[1:] == (
        "ada@example.com",
        "Ada Byron",
        "evt_end_to_end_002",
    )
    assert summary.json()["total_events"] == 2
    assert summary.json()["by_processing_status"]["processed"] == 2
    assert latest_event.status_code == 200
    assert latest_event.json()["processing_status"] == "processed"


def test_failed_handoff_recovers_after_restart_without_duplicate_contact(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "recovery.sqlite3"
    clock = ControlledClock()
    first_adapter = ScriptedCRMAdapter(
        database_path,
        fail_on_calls={1},
        apply_before_failure=True,
    )

    with TestClient(make_e2e_app(database_path, first_adapter, clock)) as first_client:
        accepted = first_client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        pending_detail = first_client.get(
            f"/internal/events/{VALID_ENQUIRY['event_id']}"
        )
        pending_summary = first_client.get("/internal/summary")

    scheduled_retry = (clock.now + timedelta(seconds=30)).isoformat(
        timespec="microseconds"
    )
    assert accepted.status_code == 202
    assert accepted.json()["processing_status"] == "pending_retry"
    assert pending_detail.json()["processing_status"] == "pending_retry"
    assert pending_detail.json()["attempt_count"] == 1
    assert pending_detail.json()["next_retry_at"] == scheduled_retry
    assert pending_detail.json()["last_failure_reason"] == SAFE_FAILURE_REASON
    assert pending_summary.json()["pending_retries"] == 1
    assert read_event_state(database_path, VALID_ENQUIRY["event_id"]) == (
        "pending_retry",
        1,
        scheduled_retry,
        SAFE_FAILURE_REASON,
    )
    assert len(read_contacts(database_path)) == 1

    clock.advance(30)
    restarted_adapter = ScriptedCRMAdapter(database_path)
    with TestClient(
        make_e2e_app(database_path, restarted_adapter, clock)
    ) as restarted_client:
        persisted_detail = restarted_client.get(
            f"/internal/events/{VALID_ENQUIRY['event_id']}"
        )
        retried = restarted_client.post("/internal/retries/process-due")
        final_detail = restarted_client.get(
            f"/internal/events/{VALID_ENQUIRY['event_id']}"
        )
        final_list = restarted_client.get(
            "/internal/events?processing_status=processed"
        )
        final_summary = restarted_client.get("/internal/summary")

    assert persisted_detail.json()["processing_status"] == "pending_retry"
    assert persisted_detail.json()["next_retry_at"] == scheduled_retry
    assert retried.status_code == 200
    assert retried.json() == {
        "claimed": 1,
        "processed": 1,
        "rescheduled": 0,
        "failed": 0,
    }
    assert restarted_adapter.call_count == 1
    assert final_detail.json()["processing_status"] == "processed"
    assert final_detail.json()["attempt_count"] == 2
    assert final_detail.json()["next_retry_at"] is None
    assert final_detail.json()["last_failure_reason"] == SAFE_FAILURE_REASON
    assert final_list.json()["total"] == 1
    assert final_list.json()["events"][0]["event_id"] == VALID_ENQUIRY["event_id"]
    assert final_summary.json()["pending_retries"] == 0
    assert final_summary.json()["by_processing_status"]["processed"] == 1
    assert read_event_state(database_path, VALID_ENQUIRY["event_id"]) == (
        "processed",
        2,
        None,
        SAFE_FAILURE_REASON,
    )
    assert len(read_contacts(database_path)) == 1


def test_exhausted_failures_remain_visible_as_terminal_failure(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "exhausted.sqlite3"
    clock = ControlledClock()
    adapter = ScriptedCRMAdapter(database_path, fail_on_calls={1, 2, 3})

    with TestClient(make_e2e_app(database_path, adapter, clock)) as client:
        first = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        clock.advance(30)
        second = client.post("/internal/retries/process-due")
        clock.advance(60)
        third = client.post("/internal/retries/process-due")
        failed_detail = client.get(
            f"/internal/events/{VALID_ENQUIRY['event_id']}"
        )
        failed_list = client.get("/internal/events?processing_status=failed")
        summary = client.get("/internal/summary")

    assert first.json()["processing_status"] == "pending_retry"
    assert second.json()["rescheduled"] == 1
    assert third.json() == {
        "claimed": 1,
        "processed": 0,
        "rescheduled": 0,
        "failed": 1,
    }
    assert adapter.call_count == 3
    assert failed_detail.status_code == 200
    assert failed_detail.json()["processing_status"] == "failed"
    assert failed_detail.json()["attempt_count"] == 3
    assert failed_detail.json()["next_retry_at"] is None
    assert failed_detail.json()["last_failure_reason"] == SAFE_FAILURE_REASON
    assert failed_list.json()["total"] == 1
    assert summary.json()["total_events"] == 1
    assert summary.json()["terminal_failures"] == 1
    assert summary.json()["pending_retries"] == 0
    assert read_event_state(database_path, VALID_ENQUIRY["event_id"]) == (
        "failed",
        3,
        None,
        SAFE_FAILURE_REASON,
    )
    assert len(read_contacts(database_path)) == 0


def test_recovery_processes_event_left_in_received_state(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stranded_received.sqlite3"
    clock = ControlledClock()

    database = EnquiryDatabase(database_path)
    database.initialize()

    adapter = SQLiteSimulatedCRMAdapter(database_path)
    adapter.initialize()

    recorded = database.record(VALID_ENQUIRY, now=clock.now)
    assert recorded.outcome == "accepted"
    assert recorded.event.processing_status == "received"
    assert recorded.event.attempt_count == 0

    processor = RetryProcessor(database, adapter, clock)
    result = processor.process_due()

    assert result == {
        "claimed": 1,
        "processed": 1,
        "rescheduled": 0,
        "failed": 0,
    }
    assert read_event_state(database_path, VALID_ENQUIRY["event_id"]) == (
        "processed",
        1,
        None,
        None,
    )
    assert len(read_contacts(database_path)) == 1
