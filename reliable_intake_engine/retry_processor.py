"""Bounded, manually triggered processing for failed CRM handoffs."""

from datetime import datetime, timedelta, timezone
from typing import Callable

from .crm_adapter import CRMAdapter
from .database import EnquiryDatabase

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 300
CLAIM_LEASE_SECONDS = 300
MAX_EVENTS_PER_TRIGGER = 100
SAFE_FAILURE_REASON = (
    "CRM handoff failed; adapter exception details are withheld."
)

Clock = Callable[[], datetime]


def current_utc_time() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def retry_delay_seconds(attempt_count: int) -> int:
    """Return an exponentially increasing, capped retry delay."""
    delay = BASE_BACKOFF_SECONDS * (2 ** max(0, attempt_count - 1))
    return min(delay, MAX_BACKOFF_SECONDS)


class RetryProcessor:
    """Claim and process due retries without waiting inside an HTTP request."""

    def __init__(
        self,
        database: EnquiryDatabase,
        adapter: CRMAdapter,
        clock: Clock = current_utc_time,
    ) -> None:
        self.database = database
        self.adapter = adapter
        self.clock = clock

    def process_due(self) -> dict[str, int]:
        """Process each due event once and return a safe summary."""
        now = self.clock()
        terminalized = self.database.terminalize_expired_attempts(
            now, MAX_ATTEMPTS
        )
        result = {
            "claimed": 0,
            "processed": 0,
            "rescheduled": 0,
            "failed": terminalized,
        }

        for _ in range(MAX_EVENTS_PER_TRIGGER):
            now = self.clock()
            event = self.database.claim_next_due_retry(
                now,
                now + timedelta(seconds=CLAIM_LEASE_SECONDS),
                MAX_ATTEMPTS,
            )
            if event is None:
                break

            result["claimed"] += 1
            try:
                self.adapter.create_or_update_contact(event)
            except Exception:
                retry_at = None
                if event.attempt_count < MAX_ATTEMPTS:
                    retry_at = self.clock() + timedelta(
                        seconds=retry_delay_seconds(event.attempt_count)
                    )
                final_status = self.database.mark_handoff_failed(
                    event.id, SAFE_FAILURE_REASON, retry_at
                )
                if final_status == "pending_retry":
                    result["rescheduled"] += 1
                else:
                    result["failed"] += 1
            else:
                self.database.mark_handoff_succeeded(event.id)
                result["processed"] += 1

        return result
