# Reliable Intake Engine

Reliable Intake Engine is a beginner-friendly FastAPI prototype for a
customer-enquiry-to-CRM integration engine. Stage 6 validates and records
enquiries, schedules bounded retries after failed handoffs, and provides
opt-in, read-only operational inspection endpoints.

## Requirements

- Python 3.12 or newer

## Install dependencies

From the project directory, create and activate a virtual environment, then
install the dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Activate the virtual environment again with `source .venv/bin/activate` in each
new terminal session before running the server or tests.

## Start the development server

```bash
python -m uvicorn reliable_intake_engine.main:app --reload
```

The server runs at `http://127.0.0.1:8000` by default.

## Submit a customer enquiry

Send a JSON request to `POST /webhooks/enquiries`:

```bash
curl -X POST http://127.0.0.1:8000/webhooks/enquiries \
  -H "Content-Type: application/json" \
  -d '{
    "event_id": "evt_2026_001",
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "phone": "+44 7700 900123",
    "message": "Please contact me about your service.",
    "source": "website"
  }'
```

A valid enquiry returns HTTP `202 Accepted`:

```json
{
  "status": "accepted",
  "event_id": "evt_2026_001",
  "message": "Enquiry accepted.",
  "processing_status": "processed"
}
```

The enquiry is stored in a local SQLite database. By default, the database file
is `data/reliable_intake_engine.sqlite3` in the project workspace; the directory
is created automatically when the application starts. To use another file, set
`RELIABLE_INTAKE_DB_PATH` before starting the server:

```bash
RELIABLE_INTAKE_DB_PATH=/path/to/enquiries.sqlite3 \
  python -m uvicorn reliable_intake_engine.main:app --reload
```

The `enquiry_events` table stores validated enquiry fields, processing status,
attempt count, UTC retry schedule, sanitized failure reason, and claim lease.
The application adds retry columns to an existing Stage 4 database with
`ALTER TABLE`; it does not drop or recreate existing event or contact records.
The `simulated_crm_contacts` table stores contacts created or updated by the
local adapter. Local SQLite files are excluded from version control.

## Simulated CRM handoff

The route, event database, and CRM adapter are separate components. The
replaceable `CRMAdapter` protocol defines the contact operation;
`SQLiteSimulatedCRMAdapter` implements it with the configured SQLite file and
makes no external network calls.

The adapter matches contacts by trimmed, case-folded email. A new email creates
a contact with a unique contact ID. For subsequent events with the same
normalized email, the event with the greater SQLite event-record ID wins: its
name, email, phone, message, and source replace the contact fields. Missing
optional fields clear the existing value. The contact ID and original creation
timestamp remain unchanged.

The event is committed before the adapter is called. A successful handoff
marks it `processed`. If the adapter fails, the event remains stored and is
marked `pending_retry` with a persisted retry time. Retry calls use the same
normalized-email upsert, so an uncertain handoff that already created a
simulated contact does not create a second contact.

## Retry policy

The maximum is **three total handoff attempts**, including the first attempt.
After a failure on attempt 1, the next attempt is due after 30 seconds; after a
failure on attempt 2, it is due after 60 seconds. Exponential backoff is capped
at five minutes. A failure on attempt 3 marks the event `failed` (terminal) and
keeps it in SQLite for inspection. The stored failure reason is generic and
does not include exception text or customer-submitted fields.

The processing states are:

- `received`: durably stored and not yet claimed for handoff.
- `processing`: an attempt is in progress; a five-minute lease prevents
  competing retry triggers from claiming the same event at once.
- `pending_retry`: a failed attempt has a future UTC retry time.
- `processed`: the adapter completed successfully.
- `failed`: retries are exhausted, or an older Stage 4 event was already
  marked failed. This state is terminal and is not retried.

There is no automatic scheduler and retry times are never waited out inside a
webhook request. A successful retry clears its schedule but retains the latest
sanitized failure reason for inspection. Each manual trigger handles at most
100 due events, keeping request work bounded. To enable the development-only
manual trigger, start the app with:

```bash
RELIABLE_INTAKE_ENABLE_RETRY_ENDPOINT=true \
  python -m uvicorn reliable_intake_engine.main:app --reload
```

Then call the endpoint when retries may be due:

```bash
curl -X POST http://127.0.0.1:8000/internal/retries/process-due
```

The endpoint is not registered unless explicitly enabled. It is intended only
for a trusted local development environment and has no authentication in this
prototype. Each call processes due work and returns counts of claimed,
processed, rescheduled, and terminally failed events. If more than 100 events
are due, call it again to process the remainder. Repeating a webhook with
the same event ID continues to return HTTP `200` and does not trigger retries;
retry scheduling is separate from webhook idempotency.

## Internal operational endpoints

The read-only `GET /internal/events`, `GET /internal/events/{event_id}`, and
`GET /internal/summary` endpoints are registered only when
`RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS=true`:

```bash
RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS=true \
  python -m uvicorn reliable_intake_engine.main:app --reload
```

This flag is separate from `RELIABLE_INTAKE_ENABLE_RETRY_ENDPOINT`; enabling the
read-only endpoints does not enable retry processing, or vice versa. If the
internal flag is unset or not `true`, these routes are not registered and
return HTTP `404`.

These endpoints have **no authentication**. Enable them only in a trusted
development environment; do not expose them publicly.

### List events

`GET /internal/events` returns summaries ordered newest first. `limit` defaults
to 20 and must be between 1 and 100; `offset` defaults to 0 and cannot be
negative. The optional `processing_status` filter accepts `received`,
`processing`, `pending_retry`, `processed`, or `failed`. Invalid parameters
return HTTP `422`.

```http
GET /internal/events?limit=20&offset=0&processing_status=pending_retry
```

```json
{
  "total": 1,
  "limit": 20,
  "offset": 0,
  "events": [
    {
      "event_id": "evt_2026_001",
      "processing_status": "pending_retry",
      "attempt_count": 1,
      "created_at": "2026-10-16T12:00:00+00:00",
      "next_retry_at": "2026-10-16T12:00:30+00:00"
    }
  ]
}
```

List results omit names, email addresses, phone numbers, and enquiry messages.

### Inspect one event

`GET /internal/events/{event_id}` returns HTTP `404` when the event does not
exist. It returns only operational fields, not the customer payload:

```json
{
  "event_id": "evt_2026_001",
  "processing_status": "failed",
  "attempt_count": 3,
  "created_at": "2026-10-16T12:00:00+00:00",
  "next_retry_at": null,
  "last_failure_reason": "CRM handoff failed; adapter exception details are withheld."
}
```

### View aggregate counts

`GET /internal/summary` returns counts directly from the SQLite event table:

```json
{
  "total_events": 4,
  "by_processing_status": {
    "received": 0,
    "processing": 1,
    "pending_retry": 2,
    "processed": 0,
    "failed": 1
  },
  "pending_retries": 2,
  "terminal_failures": 1
}
```

The event ID and sanitized failure reason are included for operational
diagnosis. These endpoints do not return email addresses, phone numbers,
messages, or other enquiry payload fields.

The required `event_id`, `name`, and `email` values are trimmed and validated.
Optional fields are `phone`, `message`, and `source`. Missing or invalid
required fields, invalid email addresses, and malformed JSON return HTTP `422`
with FastAPI's standard `detail` validation response. For example, an invalid
email address:

```bash
curl -X POST http://127.0.0.1:8000/webhooks/enquiries \
  -H "Content-Type: application/json" \
  -d '{"event_id":"evt_bad_email","name":"Example","email":"not-an-email"}'
```

returns HTTP `422 Unprocessable Entity`:

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "email"],
      "msg": "value is not a valid email address: An email address must have an @-sign.",
      "input": "not-an-email",
      "ctx": {
        "reason": "An email address must have an @-sign."
      }
    }
  ]
}
```

Malformed JSON also returns HTTP `422`. Repeating the same `event_id` with
identical validated data returns HTTP `200` and does not create another row:

```json
{
  "status": "already_received",
  "event_id": "evt_2026_001",
  "message": "This event was already received; no duplicate was created.",
  "processing_status": "processed"
}
```

Reusing an `event_id` with different data returns HTTP `409 Conflict`; the
original record is preserved:

```json
{
  "status": "conflict",
  "event_id": "evt_2026_001",
  "message": "This event ID was already received with different enquiry data.",
  "processing_status": "processed"
}
```

Accepted, duplicate, and conflict responses include `status`, `event_id`,
`message`, and `processing_status`. When the first handoff attempt fails, the
event remains stored and the route returns HTTP `202` with
`processing_status: "pending_retry"`:

```json
{
  "status": "accepted",
  "event_id": "evt_2026_001",
  "message": "Enquiry accepted; a CRM retry was scheduled.",
  "processing_status": "pending_retry"
}
```

An identical repeat while an event is pending returns HTTP `200` with
`processing_status: "pending_retry"` and does not trigger a retry. After the
third failed attempt, repeats report `processing_status: "failed"`. Validation
failures retain FastAPI's standard 422 `detail` response. Events are written
only to the local simulated CRM; no external CRM or API is contacted.

## Run the local demonstration

Prerequisites are Python 3.12 or newer and the project dependencies installed
from the project root:

```bash
python -m pip install -r requirements.txt
```

Run the demonstration with:

```bash
python -m reliable_intake_engine.demo
```

It uses FastAPI's in-process test client, synthetic enquiry data, an injected
simulated adapter, and a controllable clock. No public server is started and no
real retry delay is awaited. The command creates a new SQLite file in a
temporary directory, refuses an existing database path, and removes the
temporary file when it exits. The development database path is not used.

Expected output:

```text
1. Valid enquiry: HTTP 202; state=processed; attempts=1; contacts=1
2. Identical repeat: HTTP 200; events=1; contacts=1
3. Temporary CRM failure: HTTP 202; state=pending_retry; attempts=1; contacts=2
4. Due retry (+30s simulated): HTTP 200; state=processed; attempts=2; contacts=2
5. Retry limit exhausted: state=failed; attempts=3; contacts=2; terminal_failures=1
Demo complete. The temporary SQLite file is removed when the command exits.
```

This demonstrates local intake, event idempotency, SQLite contact upserts,
persisted retry state, simulated retry timing, and terminal failure inspection.
It does not test an external CRM, real network behavior, production security,
deployment, or actual delivery to a customer.

## Run the tests

```bash
python -m pytest
```
