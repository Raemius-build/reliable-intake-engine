"""Replaceable contact adapter and its local SQLite-backed simulation."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator, Literal, Protocol

from .database import EnquiryEvent


class CRMAdapterError(RuntimeError):
    """Raised when the CRM adapter cannot complete a contact operation."""


@dataclass(frozen=True)
class ContactUpsertResult:
    """The contact affected by an adapter operation."""

    contact_id: int
    action: Literal["created", "updated", "unchanged"]


class CRMAdapter(Protocol):
    """Interface required by the enquiry handoff."""

    def initialize(self) -> None:
        """Prepare adapter storage or resources before serving requests."""
        ...

    def create_or_update_contact(self, event: EnquiryEvent) -> ContactUpsertResult:
        """Create or update a contact for a persisted enquiry event."""
        ...


class SQLiteSimulatedCRMAdapter:
    """Simulate CRM contacts in SQLite without making external API calls."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        """Create the simulated CRM contact table if it does not exist."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS simulated_crm_contacts (
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

    def create_or_update_contact(self, event: EnquiryEvent) -> ContactUpsertResult:
        """Upsert a contact using normalized email as its identity."""
        normalized_email = event.email.strip().casefold()
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")

        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO simulated_crm_contacts (
                        normalized_email, email, name, phone, message, source,
                        last_event_sequence, last_event_id, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(normalized_email) DO NOTHING
                    """,
                    (
                        normalized_email,
                        event.email,
                        event.name,
                        event.phone,
                        event.message,
                        event.source,
                        event.id,
                        event.event_id,
                        now,
                        now,
                    ),
                )

                if cursor.rowcount == 1:
                    return ContactUpsertResult(int(cursor.lastrowid), "created")

                existing = connection.execute(
                    """
                    SELECT contact_id, last_event_sequence
                    FROM simulated_crm_contacts
                    WHERE normalized_email = ?
                    """,
                    (normalized_email,),
                ).fetchone()
                if existing is None:
                    raise CRMAdapterError(
                        "The simulated CRM contact could not be found after an email conflict."
                    )

                contact_id = existing["contact_id"]
                if event.id <= existing["last_event_sequence"]:
                    return ContactUpsertResult(contact_id, "unchanged")

                update = connection.execute(
                    """
                    UPDATE simulated_crm_contacts
                    SET email = ?, name = ?, phone = ?, message = ?, source = ?,
                        last_event_sequence = ?, last_event_id = ?, updated_at = ?
                    WHERE contact_id = ? AND last_event_sequence < ?
                    """,
                    (
                        event.email,
                        event.name,
                        event.phone,
                        event.message,
                        event.source,
                        event.id,
                        event.event_id,
                        now,
                        contact_id,
                        event.id,
                    ),
                )
                action: Literal["updated", "unchanged"] = (
                    "updated" if update.rowcount == 1 else "unchanged"
                )
                return ContactUpsertResult(contact_id, action)
        except sqlite3.Error as error:
            raise CRMAdapterError("The simulated CRM database operation failed.") from error

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
