"""Tests for the opt-in, read-only internal operational API."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reliable_intake_engine.main import create_app

VALID_STATUSES = (
    "received",
    "processing",
    "pending_retry",
    "processed",
    "failed",
)
SAFE_FAILURE_REASON = "CRM handoff failed; adapter exception details are withheld."


def make_client(
    database_path: Path,
    *,
    enable_internal_endpoints: bool | None = True,
    enable_retry_endpoint: bool | None = False,
) -> TestClient:
    return TestClient(
        create_app(
            database_path,
            enable_internal_endpoints=enable_internal_endpoints,
            enable_retry_endpoint=enable_retry_endpoint,
        )
    )


def insert_events(database_path: Path, statuses: list[str]) -> list[str]:
    """Insert events containing obvious personal data for redaction assertions."""
    event_ids = [f"internal-test-{index:03d}" for index in range(len(statuses))]
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO enquiry_events (
                event_id, name, email, phone, message, source,
                processing_status, created_at, attempt_count, next_retry_at,
                last_failure_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    event_id,
                    f"Private Customer Name {index}",
                    f"private-{index}@example.invalid",
                    f"+1-555-010{index:02d}",
                    f"Private message content {index}",
                    "test",
                    processing_status,
                    f"2026-10-16T12:{index % 60:02d}:00+00:00",
                    2 if processing_status in ("pending_retry", "failed") else 1,
                    (
                        "2026-10-16T12:30:00+00:00"
                        if processing_status == "pending_retry"
                        else None
                    ),
                    SAFE_FAILURE_REASON if processing_status == "failed" else None,
                )
                for index, (event_id, processing_status) in enumerate(
                    zip(event_ids, statuses, strict=True)
                )
            ],
        )
    return event_ids


def test_event_list_is_empty_by_default(tmp_path: Path) -> None:
    database_path = tmp_path / "empty.sqlite3"
    with make_client(database_path) as client:
        response = client.get("/internal/events")

    assert response.status_code == 200
    assert response.json() == {
        "total": 0,
        "limit": 20,
        "offset": 0,
        "events": [],
    }


def test_event_list_pagination_and_maximum_limit(tmp_path: Path) -> None:
    database_path = tmp_path / "pagination.sqlite3"
    with make_client(database_path) as client:
        event_ids = insert_events(database_path, ["processed"] * 105)

        default_page = client.get("/internal/events")
        middle_page = client.get("/internal/events?limit=2&offset=1")
        maximum_page = client.get("/internal/events?limit=100")
        over_limit = client.get("/internal/events?limit=101")

    assert default_page.status_code == 200
    assert default_page.json()["total"] == 105
    assert default_page.json()["limit"] == 20
    assert default_page.json()["offset"] == 0
    assert len(default_page.json()["events"]) == 20
    assert middle_page.json()["events"][0]["event_id"] == event_ids[-2]
    assert middle_page.json()["events"][1]["event_id"] == event_ids[-3]
    assert maximum_page.status_code == 200
    assert maximum_page.json()["limit"] == 100
    assert len(maximum_page.json()["events"]) == 100
    assert over_limit.status_code == 422


@pytest.mark.parametrize("processing_status", VALID_STATUSES)
def test_event_list_filter_matches_each_valid_status(
    tmp_path: Path,
    processing_status: str,
) -> None:
    database_path = tmp_path / f"filter-{processing_status}.sqlite3"
    with make_client(database_path) as client:
        insert_events(database_path, list(VALID_STATUSES))
        response = client.get(
            "/internal/events",
            params={"processing_status": processing_status},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["events"]) == 1
    assert body["events"][0]["processing_status"] == processing_status


@pytest.mark.parametrize(
    "query",
    [
        "?processing_status=unknown",
        "?offset=-1",
        "?limit=0",
        "?limit=101",
    ],
)
def test_invalid_event_list_query_returns_422(
    tmp_path: Path,
    query: str,
) -> None:
    with make_client(tmp_path / "invalid-query.sqlite3") as client:
        response = client.get(f"/internal/events{query}")

    assert response.status_code == 422


def test_event_details_are_redacted_and_unknown_event_returns_404(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "details.sqlite3"
    with make_client(database_path) as client:
        event_id = insert_events(database_path, ["failed"])[0]
        response = client.get(f"/internal/events/{event_id}")
        missing = client.get("/internal/events/no-such-event")

    assert response.status_code == 200
    assert response.json() == {
        "event_id": event_id,
        "processing_status": "failed",
        "attempt_count": 2,
        "created_at": "2026-10-16T12:00:00+00:00",
        "next_retry_at": None,
        "last_failure_reason": SAFE_FAILURE_REASON,
    }
    assert missing.status_code == 404
    response_text = response.text
    for personal_value in (
        "Private Customer Name",
        "private-0@example.invalid",
        "+1-555-01000",
        "Private message content",
    ):
        assert personal_value not in response_text


def test_event_list_excludes_customer_payload_fields(tmp_path: Path) -> None:
    database_path = tmp_path / "redaction.sqlite3"
    with make_client(database_path) as client:
        insert_events(database_path, ["pending_retry"])
        response = client.get("/internal/events")

    assert response.status_code == 200
    event = response.json()["events"][0]
    assert set(event) == {
        "event_id",
        "processing_status",
        "attempt_count",
        "created_at",
        "next_retry_at",
    }
    for personal_value in (
        "Private Customer Name",
        "private-0@example.invalid",
        "+1-555-01000",
        "Private message content",
    ):
        assert personal_value not in response.text


def test_summary_counts_match_database_states(tmp_path: Path) -> None:
    database_path = tmp_path / "summary.sqlite3"
    statuses = [
        "received",
        "processing",
        "pending_retry",
        "pending_retry",
        "processed",
        "failed",
        "failed",
    ]
    with make_client(database_path) as client:
        insert_events(database_path, statuses)
        response = client.get("/internal/summary")

    assert response.status_code == 200
    assert response.json() == {
        "total_events": 7,
        "by_processing_status": {
            "received": 1,
            "processing": 1,
            "pending_retry": 2,
            "processed": 1,
            "failed": 2,
        },
        "pending_retries": 2,
        "terminal_failures": 2,
    }


@pytest.mark.parametrize("flag_value", [None, "false"])
def test_internal_endpoints_are_absent_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag_value: str | None,
) -> None:
    if flag_value is None:
        monkeypatch.delenv("RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS", raising=False)
    else:
        monkeypatch.setenv("RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS", flag_value)
    monkeypatch.delenv("RELIABLE_INTAKE_ENABLE_RETRY_ENDPOINT", raising=False)

    with TestClient(create_app(tmp_path / "disabled.sqlite3")) as client:
        responses = (
            client.get("/internal/events"),
            client.get("/internal/events/example"),
            client.get("/internal/summary"),
        )

    assert [response.status_code for response in responses] == [404, 404, 404]


def test_internal_endpoints_enable_from_environment_without_enabling_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS", "true")
    monkeypatch.delenv("RELIABLE_INTAKE_ENABLE_RETRY_ENDPOINT", raising=False)

    with TestClient(create_app(tmp_path / "enabled.sqlite3")) as client:
        event_list = client.get("/internal/events")
        summary = client.get("/internal/summary")
        retry_trigger = client.post("/internal/retries/process-due")

    assert event_list.status_code == 200
    assert summary.status_code == 200
    assert retry_trigger.status_code == 404


def test_retry_endpoint_opt_in_remains_separate_from_internal_read_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS", "false")
    monkeypatch.setenv("RELIABLE_INTAKE_ENABLE_RETRY_ENDPOINT", "true")

    with TestClient(create_app(tmp_path / "retry-only.sqlite3")) as client:
        event_list = client.get("/internal/events")
        summary = client.get("/internal/summary")
        retry_trigger = client.post("/internal/retries/process-due")

    assert event_list.status_code == 404
    assert summary.status_code == 404
    assert retry_trigger.status_code == 200
    assert retry_trigger.json() == {
        "claimed": 0,
        "processed": 0,
        "rescheduled": 0,
        "failed": 0,
    }
