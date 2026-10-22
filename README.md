# Reliable Intake Engine

A Python/FastAPI prototype for reliable customer-enquiry intake and CRM handoff.

The engine validates incoming webhook events, persists them in SQLite, prevents duplicate event creation, and schedules bounded retries when a CRM handoff fails. A replaceable adapter interface separates intake processing from the CRM implementation.

**Current scope:** The CRM is simulated locally using SQLite. This project does not connect to an external CRM, run an automatic retry scheduler, or establish production readiness.

## Features

- Validated webhook intake with required-field and email validation.
- Persistent event storage using SQLite.
- Idempotency checks for repeated event IDs, including conflicting payload detection.
- Simulated CRM contact creation and updates using normalized email addresses.
- Bounded retries with persisted schedules and terminal failure states.
- Opt-in operational endpoints for inspecting event status and aggregate counts.
- Automated tests and a synthetic end-to-end demonstration.

## Architecture

```text
Webhook
   |
   v
Validation and idempotency checks
   |
   v
SQLite event store
   |
   v
CRM adapter interface
   |
   +---- Success ------> processed
   |
   +---- Temporary failure --> pending_retry
   |                              |
   |                              v
   |                       Manual retry trigger
   |                              |
   +---- Retry limit reached ----> failed
```

The event is committed before the CRM adapter is called. The included `SQLiteSimulatedCRMAdapter` makes no external network calls.

## Requirements

- Python 3.12 or newer
- Git, if cloning the repository

## Installation

Clone the repository and enter the project directory:

```bash
git clone https://github.com/Raemius-build/reliable-intake-engine.git
cd reliable-intake-engine
```

Create a virtual environment.

**Linux or macOS:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows PowerShell:**

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

Install the project dependencies:

```bash
python -m pip install -r requirements.txt
```

Activate the virtual environment again in each new terminal session before running the application or tests.

## Run the development server

From the project root:

```bash
python -m uvicorn reliable_intake_engine.main:app --reload
```

The default local address is `http://127.0.0.1:8000`.

FastAPI's interactive API documentation is available at `http://127.0.0.1:8000/docs` while the server is running.

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

A successfully handled new enquiry returns HTTP `202 Accepted`. For example:

```json
{
  "status": "accepted",
  "event_id": "evt_2026_001",
  "message": "Enquiry accepted.",
  "processing_status": "processed"
}
```

A new event may instead return `processing_status: "pending_retry"` if the adapter fails on the first attempt.

### Idempotency and validation

The `event_id` identifies an incoming event.

| Situation | Expected behavior |
|---|---|
| New, valid event | Stored and handed to the simulated CRM |
| Repeated event ID with identical validated data | HTTP `200`; no duplicate event is created |
| Repeated event ID with different validated data | HTTP `409 Conflict`; original event is preserved |
| Invalid required fields or email | HTTP `422 Unprocessable Entity` |
| Malformed JSON | HTTP `422 Unprocessable Entity` |

Repeating a webhook does not trigger a retry. Retry processing is a separate operation.

Required fields are `event_id`, `name`, and `email`. Optional fields are `phone`, `message`, and `source`. Required string values are trimmed and validated.

### Local database

By default, the application stores its SQLite database at:

```text
data/reliable_intake_engine.sqlite3
```

The directory is created automatically when the application starts. To use a different database path, set `RELIABLE_INTAKE_DB_PATH` before launching the server.

**Linux or macOS:**

```bash
RELIABLE_INTAKE_DB_PATH=/path/to/enquiries.sqlite3 \
  python -m uvicorn reliable_intake_engine.main:app --reload
```

**Windows PowerShell:**

```powershell
$env:RELIABLE_INTAKE_DB_PATH = "C:\path\to\enquiries.sqlite3"
python -m uvicorn reliable_intake_engine.main:app --reload
```

The `enquiry_events` table stores validated event data, processing status, attempt count, UTC retry schedule, sanitized failure reason, and claim lease. The `simulated_crm_contacts` table stores contacts created or updated by the local adapter.

Retry-related columns are added to an existing Stage 4 database using `ALTER TABLE`; existing event and contact records are not dropped or recreated. Local SQLite database files are excluded from version control.

## Simulated CRM adapter

The route, event database, and CRM adapter are separate components. The `CRMAdapter` protocol defines the contact operation, while `SQLiteSimulatedCRMAdapter` implements it using the configured SQLite database.

Contacts are matched by trimmed, case-folded email addresses. A new normalized email creates a contact with a unique contact ID. For subsequent events with the same normalized email, the event with the greater SQLite event-record ID wins. Its name, email, phone, message, and source replace the existing contact fields; missing optional fields clear their corresponding values. The contact ID and original creation timestamp remain unchanged.

The adapter uses an upsert so that an uncertain handoff that has already created a simulated contact does not create another contact when retried.

## Retry policy

The maximum is **three total handoff attempts**, including the initial attempt.

| Attempt that fails | Next action |
|---|---|
| Attempt 1 | Schedule a retry after 30 seconds |
| Attempt 2 | Schedule a retry after 60 seconds |
| Attempt 3 | Mark the event `failed` permanently |

Exponential backoff is capped at five minutes. The terminal failure remains in SQLite for inspection.

The processing states are:

- `received` — durably stored and not yet claimed for handoff.
- `processing` — an attempt is in progress; a five-minute lease helps prevent competing retry triggers from claiming the same event.
- `pending_retry` — a failed attempt has a future UTC retry time.
- `processed` — the adapter completed successfully.
- `failed` — retries are exhausted, or an older Stage 4 event was already marked failed. This state is terminal.

Retry times are persisted; the webhook request does not wait for them to elapse. There is **no automatic retry scheduler**. Each manual trigger handles at most 100 due events.

Failure reasons stored for inspection are generic and exclude exception text and customer-submitted fields. A successful retry clears its schedule but retains the latest sanitized failure reason.

### Enable manual retry processing

The retry endpoint is disabled unless explicitly enabled.

**Linux or macOS:**

```bash
RELIABLE_INTAKE_ENABLE_RETRY_ENDPOINT=true \
  python -m uvicorn reliable_intake_engine.main:app --reload
```

**Windows PowerShell:**

```powershell
$env:RELIABLE_INTAKE_ENABLE_RETRY_ENDPOINT = "true"
python -m uvicorn reliable_intake_engine.main:app --reload
```

Trigger processing:

```bash
curl -X POST http://127.0.0.1:8000/internal/retries/process-due
```

Each call reports counts of claimed, processed, rescheduled, and terminally failed events. If more than 100 events are due, call the endpoint again to process the remainder.

This endpoint has no authentication and is intended only for a trusted local development environment. Do not expose it publicly.

## Internal operational endpoints

The following read-only endpoints are disabled unless `RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS=true`:

- `GET /internal/events`
- `GET /internal/events/{event_id}`
- `GET /internal/summary`

Enable them separately from the retry endpoint.

**Linux or macOS:**

```bash
RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS=true \
  python -m uvicorn reliable_intake_engine.main:app --reload
```

**Windows PowerShell:**

```powershell
$env:RELIABLE_INTAKE_ENABLE_INTERNAL_ENDPOINTS = "true"
python -m uvicorn reliable_intake_engine.main:app --reload
```

When the flag is unset or not `true`, these routes are not registered and return HTTP `404`. Enabling read-only inspection does not enable retry processing, or vice versa.

These endpoints have **no authentication**. Use them only in a trusted development environment; never expose them publicly.

### List events

`GET /internal/events` returns event summaries ordered newest first.

- `limit`: defaults to 20; valid range is 1–100.
- `offset`: defaults to 0; must be non-negative.
- `processing_status`: optional filter accepting `received`, `processing`, `pending_retry`, `processed`, or `failed`.

Invalid parameters return HTTP `422`.

Example:

```http
GET /internal/events?limit=20&offset=0&processing_status=pending_retry
```

List results omit names, email addresses, phone numbers, and enquiry messages.

### Inspect one event

`GET /internal/events/{event_id}` returns HTTP `404` if the event does not exist. It exposes operational fields rather than the customer payload, including processing status, attempt count, retry schedule, and sanitized failure reason.

### View aggregate counts

`GET /internal/summary` returns counts from the SQLite event table, including total events, counts by processing status, pending retries, and terminal failures.

The endpoints do not return email addresses, phone numbers, messages, or other enquiry payload fields. Event IDs and sanitized failure reasons may be included for operational diagnosis.

## Run the demonstration

Run the synthetic demonstration from the project root:

```bash
python -m reliable_intake_engine.demo
```

It uses FastAPI's in-process test client, synthetic enquiry data, an injected simulated adapter, and a controllable clock. It does not start a public server or wait for real retry delays.

The demo creates a new SQLite database in a temporary directory, refuses an existing database path, and removes the temporary file when it exits. It does not use the development database path.

Expected output:

```text
1. Valid enquiry: HTTP 202; state=processed; attempts=1; contacts=1
2. Identical repeat: HTTP 200; events=1; contacts=1
3. Temporary CRM failure: HTTP 202; state=pending_retry; attempts=1; contacts=2
4. Due retry (+30s simulated): HTTP 200; state=processed; attempts=2; contacts=2
5. Retry limit exhausted: state=failed; attempts=3; contacts=2; terminal_failures=1
Demo complete. The temporary SQLite file is removed when the command exits.
```

The demonstration covers local intake, event idempotency, SQLite contact upserts, persisted retry state, simulated retry timing, and terminal failure inspection. It does not verify an external CRM, real network behavior, production security, deployment, or actual delivery to a customer.

## Run the tests

With the virtual environment activated:

```bash
python -m pytest
```

The test suite covers the implemented application behavior. Run it in your environment and confirm that it passes before relying on the project.

## Limitations and security

This is a portfolio prototype, not a production-ready integration service.

- The CRM adapter is local and simulated; no external CRM or API is contacted.
- There is no automatic retry scheduler; retry processing requires a manual trigger.
- The optional internal inspection and retry endpoints have no authentication.
- Production deployment, external network failure modes, and real CRM delivery have not been established by the local demo.
- Operational endpoints must remain disabled or be protected by appropriate authentication and network controls before any non-local deployment.

Do not use this prototype to process real customer data or expose its unauthenticated internal endpoints publicly without further security and operational hardening.