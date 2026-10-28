"""SQLite storage for validated customer enquiries and handoff state."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Literal, Mapping

RecordOutcome = Literal["accepted", "already_received", "conflict"]
ProcessingStatus = Literal[
    "received", "processing", "pending_retry", "processed", "failed"
]
PROCESSING_STATUSES: tuple[str, ...] = (
    "received",
    "processing",
    "pending_retry",
    "processed",
    "failed",
)

_PAYLOAD_FIELDS = ("event_id", "name", "email", "phone", "message", "source")


def utc_timestamp(value: datetime) -> str:
    """Format an aware datetime as a consistent UTC ISO timestamp."""
    if value.tzinfo is None:
        raise ValueError("A timezone-aware datetime is required.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class EnquiryEvent:
    """A validated event as it is stored in the local database."""

    id: int
    event_id: str
    name: str
    email: str
    phone: str | None
    message: str | None
    source: str | None
    processing_status: str
    created_at: str
    attempt_count: int
    next_retry_at: str | None
    last_failure_reason: str | None
    lease_expires_at: str | None


@dataclass(frozen=True)
class RecordResult:
    """The result of attempting to store an enquiry event."""

    outcome: RecordOutcome
    event: EnquiryEvent


class EnquiryDatabase:
    """Store validated enquiries and persistent handoff retry state in SQLite."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        """Create tables and safely add retry columns to existing databases."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS enquiry_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    phone TEXT,
                    message TEXT,
                    source TEXT,
                    processing_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    last_failure_reason TEXT,
                    lease_expires_at TEXT
                )
                """
            )
            existing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(enquiry_events)")
            }
            retry_columns = {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "next_retry_at": "TEXT",
                "last_failure_reason": "TEXT",
                "lease_expires_at": "TEXT",
            }
            for name, definition in retry_columns.items():
                if name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE enquiry_events ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_enquiry_events_due_retry
                ON enquiry_events(processing_status, next_retry_at)
                """
            )

    def record(
        self, enquiry: Mapping[str, Any], now: datetime | None = None
    ) -> RecordResult:
        """Insert a new event, recognize an identical repeat, or report a conflict."""
        values = {field: enquiry[field] for field in _PAYLOAD_FIELDS}
        created_at = utc_timestamp(now or datetime.now(timezone.utc))

        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO enquiry_events (
                    event_id, name, email, phone, message, source,
                    processing_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    values["event_id"],
                    values["name"],
                    values["email"],
                    values["phone"],
                    values["message"],
                    values["source"],
                    "received",
                    created_at,
                ),
            )

            if cursor.rowcount == 1:
                event = connection.execute(
                    """
                    SELECT * FROM enquiry_events WHERE id = ?
                    """,
                    (cursor.lastrowid,),
                ).fetchone()
                if event is None:
                    raise RuntimeError("The stored enquiry record could not be found.")
                return RecordResult("accepted", self._event_from_row(event))

            existing = connection.execute(
                "SELECT * FROM enquiry_events WHERE event_id = ?",
                (values["event_id"],),
            ).fetchone()
            if existing is None:
                raise RuntimeError("The conflicting enquiry record could not be found.")

            outcome: RecordOutcome = "already_received"
            if not all(existing[field] == values[field] for field in _PAYLOAD_FIELDS):
                outcome = "conflict"

            return RecordResult(outcome, self._event_from_row(existing))

    def list_operational_events(
        self,
        limit: int,
        offset: int,
        processing_status: ProcessingStatus | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return a private-field-free event page and its matching total."""
        where_clause = ""
        filter_params: tuple[str, ...] = ()
        if processing_status is not None:
            where_clause = " WHERE processing_status = ?"
            filter_params = (processing_status,)

        with self._connection() as connection:
            connection.execute("BEGIN")
            count_row = connection.execute(
                f"SELECT COUNT(*) AS total FROM enquiry_events{where_clause}",
                filter_params,
            ).fetchone()
            rows = connection.execute(
                """
                SELECT event_id, processing_status, attempt_count, created_at,
                       next_retry_at
                FROM enquiry_events
                """
                + where_clause
                + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*filter_params, limit, offset),
            ).fetchall()

        return int(count_row["total"]), [
            {
                "event_id": row["event_id"],
                "processing_status": row["processing_status"],
                "attempt_count": row["attempt_count"],
                "created_at": row["created_at"],
                "next_retry_at": row["next_retry_at"],
            }
            for row in rows
        ]

    def get_operational_event(
        self, event_id: str
    ) -> dict[str, Any] | None:
        """Return operational details without the customer's enquiry payload."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT event_id, processing_status, attempt_count, created_at,
                       next_retry_at, last_failure_reason
                FROM enquiry_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "event_id": row["event_id"],
            "processing_status": row["processing_status"],
            "attempt_count": row["attempt_count"],
            "created_at": row["created_at"],
            "next_retry_at": row["next_retry_at"],
            "last_failure_reason": row["last_failure_reason"],
        }

    def get_operational_summary(self) -> dict[str, Any]:
        """Aggregate persisted event counts by processing status."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT processing_status, COUNT(*) AS event_count
                FROM enquiry_events
                GROUP BY processing_status
                """
            ).fetchall()

        counts: dict[str, int] = {state: 0 for state in PROCESSING_STATUSES}
        for row in rows:
            counts[row["processing_status"]] = row["event_count"]
        return {
            "total_events": sum(counts.values()),
            "by_processing_status": counts,
            "pending_retries": counts.get("pending_retry", 0),
            "terminal_failures": counts.get("failed", 0),
        }

    def claim_initial_attempt(
        self,
        event_record_id: int,
        now: datetime,
        lease_expires_at: datetime,
    ) -> EnquiryEvent | None:
        """Atomically claim a just-recorded event for its initial handoff."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE enquiry_events
                SET processing_status = 'processing',
                    attempt_count = attempt_count + 1,
                    next_retry_at = NULL,
                    lease_expires_at = ?
                WHERE id = ? AND processing_status = 'received'
                """,
                (utc_timestamp(lease_expires_at), event_record_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM enquiry_events WHERE id = ?", (event_record_id,)
            ).fetchone()
            return self._event_from_row(row)

    def claim_next_due_retry(
        self,
        now: datetime,
        lease_expires_at: datetime,
        max_attempts: int,
    ) -> EnquiryEvent | None:
        """Claim one due retry using a write lock to prevent competing claims."""
        now_text = utc_timestamp(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM enquiry_events
                WHERE (
                    processing_status = 'received'
                    AND attempt_count < ?
                ) OR (
                    processing_status = 'pending_retry'
                    AND next_retry_at <= ?
                ) OR (
                    processing_status = 'processing'
                    AND lease_expires_at <= ?
                    AND attempt_count < ?
                )
                ORDER BY COALESCE(next_retry_at, lease_expires_at), id
                LIMIT 1
                """,
                (max_attempts, now_text, now_text, max_attempts),
            ).fetchone()
            if row is None:
                return None

            cursor = connection.execute(
                """
                UPDATE enquiry_events
                SET processing_status = 'processing',
                    attempt_count = attempt_count + 1,
                    next_retry_at = NULL,
                    lease_expires_at = ?
                WHERE id = ?
                """,
                (utc_timestamp(lease_expires_at), row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            updated = connection.execute(
                "SELECT * FROM enquiry_events WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._event_from_row(updated)

    def terminalize_expired_attempts(self, now: datetime, max_attempts: int) -> int:
        """Mark expired in-flight work terminal when its attempt budget is spent."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE enquiry_events
                SET processing_status = 'failed',
                    next_retry_at = NULL,
                    lease_expires_at = NULL,
                    last_failure_reason =
                        'The CRM handoff outcome was uncertain; no attempts remain.'
                WHERE processing_status = 'processing'
                  AND lease_expires_at <= ?
                  AND attempt_count >= ?
                """,
                (utc_timestamp(now), max_attempts),
            )
            return cursor.rowcount

    def mark_handoff_succeeded(self, event_record_id: int) -> None:
        """Mark a claimed event processed and clear its retry metadata."""
        self._update_claimed_event(
            event_record_id,
            """
            SET processing_status = 'processed',
                next_retry_at = NULL,
                lease_expires_at = NULL
            """,
        )

    def mark_handoff_failed(
        self,
        event_record_id: int,
        failure_reason: str,
        next_retry_at: datetime | None,
    ) -> Literal["pending_retry", "failed"]:
        """Persist a sanitized failure and either schedule or end retries."""
        next_status: Literal["pending_retry", "failed"] = (
            "pending_retry" if next_retry_at is not None else "failed"
        )
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE enquiry_events
                SET processing_status = ?,
                    next_retry_at = ?,
                    last_failure_reason = ?,
                    lease_expires_at = NULL
                WHERE id = ? AND processing_status = 'processing'
                """,
                (
                    next_status,
                    utc_timestamp(next_retry_at) if next_retry_at else None,
                    failure_reason,
                    event_record_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The claimed enquiry event could not be updated.")
        return next_status

    def _update_claimed_event(self, event_record_id: int, update_sql: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE enquiry_events {update_sql} "
                "WHERE id = ? AND processing_status = 'processing'",
                (event_record_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The claimed enquiry event could not be updated.")

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> EnquiryEvent:
        return EnquiryEvent(
            id=row["id"],
            event_id=row["event_id"],
            name=row["name"],
            email=row["email"],
            phone=row["phone"],
            message=row["message"],
            source=row["source"],
            processing_status=row["processing_status"],
            created_at=row["created_at"],
            attempt_count=row["attempt_count"],
            next_retry_at=row["next_retry_at"],
            last_failure_reason=row["last_failure_reason"],
            lease_expires_at=row["lease_expires_at"],
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
