"""Tests for the repeatable synthetic demonstration command."""

from io import StringIO
from pathlib import Path
import sqlite3

import pytest

from reliable_intake_engine.demo import run_demo


def test_demo_runs_all_scenarios_in_isolated_database(tmp_path: Path) -> None:
    database_path = tmp_path / "demo.sqlite3"
    output = StringIO()

    run_demo(database_path, output)

    printed = output.getvalue()
    assert "1. Valid enquiry: HTTP 202; state=processed; attempts=1; contacts=1" in printed
    assert "2. Identical repeat: HTTP 200; events=1; contacts=1" in printed
    assert (
        "3. Temporary CRM failure: HTTP 202; state=pending_retry; "
        "attempts=1; contacts=2"
    ) in printed
    assert (
        "4. Due retry (+30s simulated): HTTP 200; state=processed; "
        "attempts=2; contacts=2"
    ) in printed
    assert (
        "5. Retry limit exhausted: state=failed; attempts=3; "
        "contacts=2; terminal_failures=1"
    ) in printed
    assert "alpha@example.com" not in printed
    assert "beta@example.com" not in printed
    assert "gamma@example.com" not in printed
    assert "Demo Customer" not in printed

    with sqlite3.connect(database_path) as connection:
        events = connection.execute(
            """
            SELECT event_id, processing_status, attempt_count, next_retry_at
            FROM enquiry_events
            ORDER BY event_id
            """
        ).fetchall()
        contact_count = connection.execute(
            "SELECT COUNT(*) FROM simulated_crm_contacts"
        ).fetchone()[0]

    assert events == [
        ("demo-exhausted-001", "failed", 3, None),
        ("demo-retry-001", "processed", 2, None),
        ("demo-success-001", "processed", 1, None),
    ]
    assert contact_count == 2


def test_demo_refuses_to_use_an_existing_database_file(tmp_path: Path) -> None:
    database_path = tmp_path / "keep.sqlite3"
    original_content = b"leave existing file untouched"
    database_path.write_bytes(original_content)

    with pytest.raises(FileExistsError, match="new, isolated SQLite file"):
        run_demo(database_path, StringIO())

    assert database_path.read_bytes() == original_content
