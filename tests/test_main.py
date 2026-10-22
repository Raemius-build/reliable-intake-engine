"""Tests for the Reliable Intake Engine's HTTP endpoints."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
from threading import Barrier, Event, Lock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reliable_intake_engine.crm_adapter import (
    ContactUpsertResult,
    SQLiteSimulatedCRMAdapter,
)
from reliable_intake_engine.database import EnquiryEvent
from reliable_intake_engine.main import create_app


VALID_ENQUIRY = {
    "event_id": "evt_2026_001",
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "phone": "+44 7700 900123",
    "message": "Please contact me about your service.",
    "source": "website",
}


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "test-enquiries.sqlite3"


@pytest.fixture
def client(database_path: Path):
    with TestClient(create_app(database_path)) as test_client:
        yield test_client


def saved_events(database_path: Path) -> list[tuple]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            """
            SELECT event_id, name, email, phone, message, source,
                   processing_status, created_at
            FROM enquiry_events
            ORDER BY id
            """
        ).fetchall()


def saved_contacts(database_path: Path) -> list[tuple]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            """
            SELECT contact_id, normalized_email, email, name, phone, message, source,
                   last_event_sequence, last_event_id, created_at, updated_at
            FROM simulated_crm_contacts
            ORDER BY contact_id
            """
        ).fetchall()


class FailingCRMAdapter:
    """Adapter that can fail selected calls, optionally after an upsert."""

    def __init__(
        self,
        database_path: Path,
        failures: int | None = None,
        apply_before_failure: bool = False,
    ) -> None:
        self.simulated_adapter = SQLiteSimulatedCRMAdapter(database_path)
        self.call_count = 0
        self.failures = failures
        self.apply_before_failure = apply_before_failure

    def initialize(self) -> None:
        self.simulated_adapter.initialize()

    def create_or_update_contact(self, event: EnquiryEvent) -> ContactUpsertResult:
        self.call_count += 1
        should_fail = self.failures is None or self.call_count <= self.failures
        if should_fail and self.apply_before_failure:
            self.simulated_adapter.create_or_update_contact(event)
        if should_fail:
            raise RuntimeError(
                f"The simulated CRM is unavailable for {event.email}."
            )
        return self.simulated_adapter.create_or_update_contact(event)


class MutableClock:
    """Simple deterministic UTC clock for retry schedule tests."""

    def __init__(self) -> None:
        self.value = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def saved_retry_state(database_path: Path) -> tuple:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            """
            SELECT processing_status, attempt_count, next_retry_at,
                   last_failure_reason, lease_expires_at
            FROM enquiry_events
            WHERE event_id = ?
            """,
            (VALID_ENQUIRY["event_id"],),
        ).fetchone()


def test_health_endpoint_reports_healthy_status(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_root_endpoint_describes_first_stage_prototype(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "service": "reliable-intake-engine",
        "description": "A customer-enquiry-to-CRM integration engine.",
        "stage": "first-stage prototype",
    }


def test_valid_enquiry_is_accepted_and_event_id_is_trimmed(client: TestClient) -> None:
    enquiry = {
        **VALID_ENQUIRY,
        "event_id": " evt_2026_001 ",
        "name": " Ada Lovelace ",
        "email": " ada@example.com ",
    }

    response = client.post("/webhooks/enquiries", json=enquiry)

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "event_id": "evt_2026_001",
        "message": "Enquiry accepted.",
        "processing_status": "processed",
    }


@pytest.mark.parametrize("missing_field", ["event_id", "name", "email"])
def test_missing_required_field_is_rejected(client: TestClient, missing_field: str) -> None:
    enquiry = {key: value for key, value in VALID_ENQUIRY.items() if key != missing_field}

    response = client.post("/webhooks/enquiries", json=enquiry)

    assert response.status_code == 422


def test_invalid_email_is_rejected(client: TestClient) -> None:
    enquiry = {**VALID_ENQUIRY, "email": "not-an-email"}

    response = client.post("/webhooks/enquiries", json=enquiry)

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["event_id", "name", "email"])
def test_whitespace_only_required_field_is_rejected(client: TestClient, field: str) -> None:
    enquiry = {**VALID_ENQUIRY, field: "   "}

    response = client.post("/webhooks/enquiries", json=enquiry)

    assert response.status_code == 422


def test_optional_fields_can_be_omitted(client: TestClient) -> None:
    enquiry = {
        "event_id": "evt_minimal",
        "name": "Grace Hopper",
        "email": "grace@example.com",
    }

    response = client.post("/webhooks/enquiries", json=enquiry)

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "event_id": "evt_minimal",
        "message": "Enquiry accepted.",
        "processing_status": "processed",
    }


def test_malformed_json_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/webhooks/enquiries",
        content='{"event_id":',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422


def test_new_event_is_persisted_with_status_and_utc_timestamp(
    client: TestClient,
    database_path: Path,
) -> None:
    response = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)

    assert response.status_code == 202
    rows = saved_events(database_path)
    assert len(rows) == 1
    assert rows[0][:7] == (
        "evt_2026_001",
        "Ada Lovelace",
        "ada@example.com",
        "+44 7700 900123",
        "Please contact me about your service.",
        "website",
        "processed",
    )
    created_at = datetime.fromisoformat(rows[0][7])
    assert created_at.utcoffset() == timezone.utc.utcoffset(created_at)


def test_identical_duplicate_is_acknowledged_without_creating_a_second_row(
    client: TestClient,
    database_path: Path,
) -> None:
    first_response = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
    second_response = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)

    assert first_response.status_code == 202
    assert second_response.status_code == 200
    assert second_response.json() == {
        "status": "already_received",
        "event_id": "evt_2026_001",
        "message": "This event was already received; no duplicate was created.",
        "processing_status": "processed",
    }
    assert len(saved_events(database_path)) == 1
    assert len(saved_contacts(database_path)) == 1


def test_reused_event_id_with_different_data_conflicts_and_preserves_original(
    client: TestClient,
    database_path: Path,
) -> None:
    changed_enquiry = {**VALID_ENQUIRY, "message": "A different message."}
    first_response = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
    conflict_response = client.post("/webhooks/enquiries", json=changed_enquiry)

    assert first_response.status_code == 202
    assert conflict_response.status_code == 409
    assert conflict_response.json() == {
        "status": "conflict",
        "event_id": "evt_2026_001",
        "message": "This event ID was already received with different enquiry data.",
        "processing_status": "processed",
    }
    rows = saved_events(database_path)
    assert len(rows) == 1
    assert rows[0][4] == VALID_ENQUIRY["message"]


def test_event_persists_across_application_restart(
    database_path: Path,
) -> None:
    with TestClient(create_app(database_path)) as first_client:
        first_response = first_client.post("/webhooks/enquiries", json=VALID_ENQUIRY)

    with TestClient(create_app(database_path)) as restarted_client:
        repeated_response = restarted_client.post("/webhooks/enquiries", json=VALID_ENQUIRY)

    assert first_response.status_code == 202
    assert repeated_response.status_code == 200
    assert repeated_response.json()["status"] == "already_received"
    assert len(saved_events(database_path)) == 1
    assert len(saved_contacts(database_path)) == 1


def test_concurrent_identical_submissions_create_only_one_event(
    client: TestClient,
    database_path: Path,
) -> None:
    submission_count = 8
    start_together = Barrier(submission_count)

    def submit_event(_: int):
        start_together.wait(timeout=10)
        return client.post("/webhooks/enquiries", json=VALID_ENQUIRY)

    with ThreadPoolExecutor(max_workers=submission_count) as executor:
        responses = list(executor.map(submit_event, range(submission_count)))

    response_codes = [response.status_code for response in responses]
    assert response_codes.count(202) == 1
    assert response_codes.count(200) == submission_count - 1
    assert len(saved_events(database_path)) == 1
    assert len(saved_contacts(database_path)) == 1
    accepted_response = next(response for response in responses if response.status_code == 202)
    assert accepted_response.json()["processing_status"] == "processed"


def test_invalid_enquiry_is_not_persisted(
    client: TestClient,
    database_path: Path,
) -> None:
    invalid_enquiry = {**VALID_ENQUIRY, "email": "not-an-email"}

    response = client.post("/webhooks/enquiries", json=invalid_enquiry)

    assert response.status_code == 422
    assert saved_events(database_path) == []


def test_new_event_creates_a_simulated_crm_contact(
    client: TestClient,
    database_path: Path,
) -> None:
    response = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)

    assert response.status_code == 202
    assert response.json()["processing_status"] == "processed"
    contacts = saved_contacts(database_path)
    assert len(contacts) == 1
    contact = contacts[0]
    assert contact[0] > 0
    assert contact[1:7] == (
        "ada@example.com",
        "ada@example.com",
        "Ada Lovelace",
        "+44 7700 900123",
        "Please contact me about your service.",
        "website",
    )
    assert contact[8] == "evt_2026_001"


def test_different_events_with_normalized_same_email_share_one_contact(
    client: TestClient,
    database_path: Path,
) -> None:
    first_response = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
    second_enquiry = {
        **VALID_ENQUIRY,
        "event_id": "evt_2026_002",
        "email": " ADA@EXAMPLE.COM ",
    }
    second_response = client.post("/webhooks/enquiries", json=second_enquiry)

    assert first_response.status_code == 202
    assert second_response.status_code == 202
    contacts = saved_contacts(database_path)
    assert len(contacts) == 1
    assert contacts[0][1] == "ada@example.com"
    assert contacts[0][8] == "evt_2026_002"


def test_newer_event_replaces_contact_fields_and_clears_omitted_values(
    client: TestClient,
    database_path: Path,
) -> None:
    client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
    latest_enquiry = {
        "event_id": "evt_2026_002",
        "name": "Ada Byron",
        "email": "ada@example.com",
        "message": "A later enquiry.",
    }

    response = client.post("/webhooks/enquiries", json=latest_enquiry)

    assert response.status_code == 202
    contacts = saved_contacts(database_path)
    assert len(contacts) == 1
    contact = contacts[0]
    assert contact[2:7] == (
        "ada@example.com",
        "Ada Byron",
        None,
        "A later enquiry.",
        None,
    )
    assert contact[8] == "evt_2026_002"
    assert contact[9] == contact[10] or contact[9] < contact[10]


def test_adapter_failure_keeps_event_and_marks_handoff_failed(
    database_path: Path,
) -> None:
    failing_adapter = FailingCRMAdapter(database_path)
    with TestClient(
        create_app(
            database_path,
            crm_adapter=failing_adapter,
            clock=MutableClock(),
            enable_retry_endpoint=True,
        )
    ) as failing_client:
        response = failing_client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        repeated_response = failing_client.post("/webhooks/enquiries", json=VALID_ENQUIRY)

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "event_id": "evt_2026_001",
        "message": "Enquiry accepted; a CRM retry was scheduled.",
        "processing_status": "pending_retry",
    }
    assert repeated_response.status_code == 200
    assert repeated_response.json()["processing_status"] == "pending_retry"
    assert failing_adapter.call_count == 1

    events = saved_events(database_path)
    assert len(events) == 1
    assert events[0][6] == "pending_retry"
    assert saved_contacts(database_path) == []


def test_transient_failure_schedules_retry_with_sanitized_metadata(
    database_path: Path,
) -> None:
    clock = MutableClock()
    adapter = FailingCRMAdapter(database_path)

    with TestClient(
        create_app(
            database_path,
            crm_adapter=adapter,
            clock=clock,
            enable_retry_endpoint=True,
        )
    ) as client:
        response = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)

    state = saved_retry_state(database_path)
    assert response.json()["processing_status"] == "pending_retry"
    assert state[:3] == (
        "pending_retry",
        1,
        (clock() + timedelta(seconds=30)).isoformat(timespec="microseconds"),
    )
    assert state[3] == (
        "CRM handoff failed; adapter exception details are withheld."
    )
    assert VALID_ENQUIRY["email"] not in state[3]
    assert state[4] is None


def test_due_retry_succeeds_and_retains_failure_history(
    database_path: Path,
) -> None:
    clock = MutableClock()
    adapter = FailingCRMAdapter(database_path, failures=1)

    with TestClient(
        create_app(
            database_path,
            crm_adapter=adapter,
            clock=clock,
            enable_retry_endpoint=True,
        )
    ) as client:
        client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        clock.advance(30)
        retry_response = client.post("/internal/retries/process-due")

    assert retry_response.status_code == 200
    assert retry_response.json() == {
        "claimed": 1,
        "processed": 1,
        "rescheduled": 0,
        "failed": 0,
    }
    assert adapter.call_count == 2
    assert saved_retry_state(database_path) == (
        "processed",
        2,
        None,
        "CRM handoff failed; adapter exception details are withheld.",
        None,
    )
    assert len(saved_contacts(database_path)) == 1


def test_retry_before_due_time_is_skipped(database_path: Path) -> None:
    clock = MutableClock()
    adapter = FailingCRMAdapter(database_path, failures=1)

    with TestClient(
        create_app(
            database_path,
            crm_adapter=adapter,
            clock=clock,
            enable_retry_endpoint=True,
        )
    ) as client:
        client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        retry_response = client.post("/internal/retries/process-due")

    assert retry_response.status_code == 200
    assert retry_response.json()["claimed"] == 0
    assert adapter.call_count == 1
    assert saved_retry_state(database_path)[0:2] == ("pending_retry", 1)


def test_attempt_limit_marks_event_terminally_failed(database_path: Path) -> None:
    clock = MutableClock()
    adapter = FailingCRMAdapter(database_path)

    with TestClient(
        create_app(
            database_path,
            crm_adapter=adapter,
            clock=clock,
            enable_retry_endpoint=True,
        )
    ) as client:
        first_response = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        clock.advance(30)
        second_attempt = client.post("/internal/retries/process-due")
        clock.advance(60)
        third_attempt = client.post("/internal/retries/process-due")
        fourth_attempt = client.post("/internal/retries/process-due")

    assert first_response.json()["processing_status"] == "pending_retry"
    assert second_attempt.json() == {
        "claimed": 1,
        "processed": 0,
        "rescheduled": 1,
        "failed": 0,
    }
    assert third_attempt.json() == {
        "claimed": 1,
        "processed": 0,
        "rescheduled": 0,
        "failed": 1,
    }
    assert fourth_attempt.json()["claimed"] == 0
    assert adapter.call_count == 3
    state = saved_retry_state(database_path)
    assert state[0:3] == ("failed", 3, None)
    assert state[3] == "CRM handoff failed; adapter exception details are withheld."
    assert state[4] is None
    assert len(saved_events(database_path)) == 1


def test_pending_retry_survives_application_restart(database_path: Path) -> None:
    clock = MutableClock()
    first_adapter = FailingCRMAdapter(database_path)
    with TestClient(
        create_app(
            database_path,
            crm_adapter=first_adapter,
            clock=clock,
            enable_retry_endpoint=True,
        )
    ) as first_client:
        first_client.post("/webhooks/enquiries", json=VALID_ENQUIRY)

    persisted_before_restart = saved_retry_state(database_path)
    assert persisted_before_restart[0:2] == ("pending_retry", 1)

    clock.advance(30)
    restarted_adapter = FailingCRMAdapter(database_path, failures=0)
    with TestClient(
        create_app(
            database_path,
            crm_adapter=restarted_adapter,
            clock=clock,
            enable_retry_endpoint=True,
        )
    ) as restarted_client:
        retry_response = restarted_client.post("/internal/retries/process-due")

    assert retry_response.json()["processed"] == 1
    assert restarted_adapter.call_count == 1
    assert saved_retry_state(database_path) == (
        "processed",
        2,
        None,
        "CRM handoff failed; adapter exception details are withheld.",
        None,
    )
    assert len(saved_events(database_path)) == 1


def test_duplicate_webhook_does_not_trigger_or_reschedule_retry(
    database_path: Path,
) -> None:
    clock = MutableClock()
    adapter = FailingCRMAdapter(database_path, failures=1)

    with TestClient(
        create_app(
            database_path,
            crm_adapter=adapter,
            clock=clock,
            enable_retry_endpoint=True,
        )
    ) as client:
        client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        before_repeat = saved_retry_state(database_path)
        duplicate = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        after_repeat = saved_retry_state(database_path)

    assert duplicate.status_code == 200
    assert duplicate.json()["processing_status"] == "pending_retry"
    assert adapter.call_count == 1
    assert after_repeat == before_repeat


class BlockingRetryAdapter:
    """Fail once, then block a retry so concurrent triggers can be tested."""

    def __init__(self, database_path: Path) -> None:
        self.simulated_adapter = SQLiteSimulatedCRMAdapter(database_path)
        self.call_count = 0
        self.active_calls = 0
        self.maximum_active_calls = 0
        self.lock = Lock()
        self.retry_started = Event()
        self.release_retry = Event()

    def initialize(self) -> None:
        self.simulated_adapter.initialize()

    def create_or_update_contact(self, event: EnquiryEvent) -> ContactUpsertResult:
        with self.lock:
            self.call_count += 1
            call_number = self.call_count
            self.active_calls += 1
            self.maximum_active_calls = max(
                self.maximum_active_calls, self.active_calls
            )
        try:
            if call_number == 1:
                raise RuntimeError("First attempt fails.")
            if call_number == 2:
                self.retry_started.set()
                if not self.release_retry.wait(timeout=5):
                    raise TimeoutError("Test did not release the retry.")
            return self.simulated_adapter.create_or_update_contact(event)
        finally:
            with self.lock:
                self.active_calls -= 1


def test_competing_retry_triggers_do_not_process_one_event_simultaneously(
    database_path: Path,
) -> None:
    clock = MutableClock()
    adapter = BlockingRetryAdapter(database_path)
    with TestClient(
        create_app(
            database_path,
            crm_adapter=adapter,
            clock=clock,
            enable_retry_endpoint=True,
        )
    ) as client:
        client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        clock.advance(30)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_trigger = executor.submit(
                client.post, "/internal/retries/process-due"
            )
            assert adapter.retry_started.wait(timeout=5)
            second_trigger = executor.submit(
                client.post, "/internal/retries/process-due"
            )
            second_response = second_trigger.result(timeout=5)
            adapter.release_retry.set()
            first_response = first_trigger.result(timeout=5)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["claimed"] == 1
    assert second_response.json()["claimed"] == 0
    assert adapter.call_count == 2
    assert adapter.maximum_active_calls == 1
    assert saved_retry_state(database_path)[0:2] == ("processed", 2)
    assert len(saved_contacts(database_path)) == 1


def test_uncertain_handoff_retry_does_not_duplicate_contact(
    database_path: Path,
) -> None:
    clock = MutableClock()
    adapter = FailingCRMAdapter(
        database_path,
        failures=1,
        apply_before_failure=True,
    )

    with TestClient(
        create_app(
            database_path,
            crm_adapter=adapter,
            clock=clock,
            enable_retry_endpoint=True,
        )
    ) as client:
        failed_response = client.post("/webhooks/enquiries", json=VALID_ENQUIRY)
        contacts_after_uncertain_result = saved_contacts(database_path)
        clock.advance(30)
        retry_response = client.post("/internal/retries/process-due")

    assert failed_response.json()["processing_status"] == "pending_retry"
    assert len(contacts_after_uncertain_result) == 1
    assert retry_response.json()["processed"] == 1
    assert adapter.call_count == 2
    contacts_after_retry = saved_contacts(database_path)
    assert len(contacts_after_retry) == 1
    assert contacts_after_retry[0][0] == contacts_after_uncertain_result[0][0]
    assert contacts_after_retry[0][8] == VALID_ENQUIRY["event_id"]


def test_startup_migrates_stage_four_schema_without_losing_records(
    database_path: Path,
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE enquiry_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT,
                message TEXT,
                source TEXT,
                processing_status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO enquiry_events (
                event_id, name, email, phone, message, source,
                processing_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                VALID_ENQUIRY["event_id"],
                VALID_ENQUIRY["name"],
                VALID_ENQUIRY["email"],
                VALID_ENQUIRY["phone"],
                VALID_ENQUIRY["message"],
                VALID_ENQUIRY["source"],
                "processed",
                "2026-10-16T12:00:00+00:00",
            ),
        )
        connection.execute(
            """
            CREATE TABLE simulated_crm_contacts (
                contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_email TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT,
                message TEXT,
                source TEXT,
                last_event_sequence INTEGER NOT NULL,
                last_event_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO simulated_crm_contacts (
                normalized_email, email, name, phone, message, source,
                last_event_sequence, last_event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ada@example.com",
                "ada@example.com",
                "Ada Lovelace",
                None,
                None,
                "website",
                1,
                VALID_ENQUIRY["event_id"],
                "2026-10-16T12:00:00+00:00",
                "2026-10-16T12:00:00+00:00",
            ),
        )

    with TestClient(create_app(database_path)):
        pass

    with sqlite3.connect(database_path) as connection:
        event_row = connection.execute(
            """
            SELECT event_id, name, processing_status, attempt_count,
                   next_retry_at, last_failure_reason
            FROM enquiry_events
            """
        ).fetchone()
        saved_contact = connection.execute(
            """
            SELECT normalized_email, name, last_event_id
            FROM simulated_crm_contacts
            """
        ).fetchone()

    assert event_row == (
        VALID_ENQUIRY["event_id"],
        VALID_ENQUIRY["name"],
        "processed",
        0,
        None,
        None,
    )
    assert saved_contact == (
        "ada@example.com",
        "Ada Lovelace",
        VALID_ENQUIRY["event_id"],
    )


def test_manual_retry_endpoint_is_not_registered_by_default(
    database_path: Path,
) -> None:
    with TestClient(create_app(database_path, enable_retry_endpoint=False)) as client:
        response = client.post("/internal/retries/process-due")

    assert response.status_code == 404
