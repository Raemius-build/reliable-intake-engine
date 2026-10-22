# First commercial use-case hypothesis

## Status and scope

This is a positioning hypothesis for the next validation step, not evidence of
market demand. No customer interviews, workflow observations, CRM comparisons,
or willingness-to-pay checks are claimed here. The proposed first segment is a
narrow starting point within the brief's initial hypothesis about small
home-services businesses: **independent residential HVAC contractors that
receive enquiries through a website form and use a separate CRM**.

The project is not ready to be sold as a production integration. This document
defines a problem to validate and a bounded pilot to consider only if that
validation supports it.

## Working hypotheses

- The target segment is independent residential HVAC contractors with a website
  enquiry form and a separate CRM.
- An owner/operator or office/operations lead is likely to care about the
  reliability of the form-to-CRM handoff and may be the buyer.
- Some businesses in this segment may have a meaningful gap in durable
  capture, duplicate-safe recovery, or visibility into failed handoffs.
- A business with that verified gap may consider a focused paid pilot.

None of these market hypotheses has been validated.

## Target business and likely buyer

**Target hypothesis:** a small, independent residential HVAC contractor with a
website enquiry form, a CRM used to track prospective customers, and an owner
or office/operations person responsible for ensuring new enquiries are
followed up.

**Likely buyer hypothesis:** the owner/operator or the person responsible for
office operations and lead handling. The person who feels the problem may be
the employee who checks the form inbox and CRM. These roles and the segment's
fit have not been verified with a real business.

Residential HVAC is a deliberately specific test segment, not a claim that it
has more failures, greater urgency, or more willingness to pay than other home
services.

## Proposed workflow and problem

The workflow to investigate is:

1. A homeowner submits a service enquiry on the contractor's website.
2. The form provider sends the enquiry to an intake service with a stable event
   identifier.
3. The service records the event and passes the agreed customer fields to one
   CRM as a contact or lead, according to the contractor's existing process.
4. If the CRM handoff fails or its outcome is uncertain, the event remains
   identifiable, eligible retries happen under an agreed policy, and an
   operator can find events that did not complete.

The problem hypothesis is a gap between website-form delivery and CRM follow-up:
a downstream failure or ambiguous response may leave an enquiry absent,
delayed, or duplicated in the CRM without a clear recovery path. Plausible
consequences include slower callbacks, staff time spent reconciling records,
duplicate follow-up, or an enquiry receiving no response. These consequences
are possibilities to check, not measured customer outcomes or established
facts.

The prototype does not ensure that the business will win or retain a job, that
every website submission reaches the intake service, or that an enquiry is
followed up promptly. It only addresses part of the handoff after an event
reaches its intake endpoint.

## Established implementation facts

The following facts are visible in the current code and documentation:

- `POST /webhooks/enquiries` accepts an event ID, name, and email, plus optional
  phone, message, and source fields. It validates the payload and records the
  event in local SQLite before calling the adapter.
- Repeating an event ID with identical validated data does not create another
  event. Reusing that ID with different data returns a conflict.
- The current adapter is a local SQLite simulation. It creates or updates a
  simulated contact using normalized email; it does not connect to an external
  CRM.
- An adapter exception leaves a recorded event in a retryable or terminal
  state. The policy allows three total attempts, with the documented first two
  retry delays of 30 and 60 seconds. The retry processor is invoked manually;
  there is no automatic scheduler.
- Operational read endpoints and the manual retry endpoint are optional
  development endpoints without authentication. They are not suitable for
  public production exposure as-is.
- The Stage 8 demo uses synthetic data, an in-process FastAPI test client, a
  temporary SQLite database, a simulated adapter, and a controllable clock. Its
  printed output omits customer fields. It does not use a real CRM or wait for
  real retry delays.

## Unknowns, simulations, and missing capabilities

Commercial unknowns include whether target businesses actually have this
workflow, whether they experience the proposed failure, how often it happens,
what consequences it causes, whether existing tools already solve it, and
whether a responsible buyer would consider paying for the proposed pilot. No
specific form provider or CRM has been selected based on customer evidence.

No external form provider or CRM has been selected or tested. Real API
authentication, field mapping, CRM rate limits and error responses, destination
deduplication behavior, and the handling of ambiguous network outcomes remain
unverified. The demo's SQLite contact behavior does not establish that a
particular CRM will behave the same way.

The application also lacks an automatically running retry scheduler, production
authentication and authorization, deployment and monitoring, operator alerts,
and validated customer-data retention and security practices. The internal
routes must not be exposed publicly in their current unauthenticated form.
Production readiness, delivery guarantees, service levels, and customer results
have not been established.

## Narrow service offer to validate

**Proposed offer:** a one-flow enquiry-to-CRM reliability pilot for one
residential HVAC contractor: connect one website enquiry source to one
customer-selected CRM, with agreed field mapping, duplicate handling, retry
behavior, and an operator-visible outcome when delivery does not complete.
This is a proposed future pilot, not a service the current prototype can
provide today.

**Inputs from the business**

- One website form/provider and one CRM, with authorized access to suitable
  non-production or test environments.
- The existing enquiry fields and the destination contact/lead fields they
  should map to.
- The current event identifier or agreement on how a stable identifier can be
  supplied.
- The current process for identifying, retrying, and escalating a failed
  handoff.
- An owner for technical access and an owner for day-to-day exception handling.

**Intended outputs**

- One configured form-to-CRM flow, limited to the agreed fields and destination
  record type.
- A durable record of intake and handoff state, duplicate-safe behavior for
  repeated deliveries, and the agreed bounded recovery policy.
- A way for an authorized operator to identify completed and terminally failed
  events without exposing enquiry contents in routine operational summaries.

**Boundaries**

- One form source, one CRM destination, and one agreed enquiry flow.
- No quoting, appointment scheduling, dispatch, marketing campaigns, customer
  response automation, lead scoring, CRM migration, or multi-CRM support.
- No promise of sales, revenue, response-time improvement, zero data loss, or
  a production availability level.
- No production launch until real integration behavior, access controls,
  monitoring, data handling, and operational ownership have been addressed.
- No price is proposed here.

**Proposed acceptance criteria for a pre-production pilot** — each item is a
pass/fail test using synthetic enquiries in the authorized test environment:

1. A valid enquiry produces one stored event and one correctly mapped
   destination record.
2. Replaying the same event with the same data leaves exactly one event and
   exactly one corresponding destination record.
3. Reusing an event ID with changed data is rejected as a conflict and does not
   overwrite the original event.
4. A simulated lost acknowledgement after the destination accepts a write is
   retried according to the agreed policy and leaves exactly one destination
   record.
5. A persistent simulated failure reaches a terminal state after the agreed
   attempt limit; an authorized operator can identify that state, and it is not
   reported as successful.
6. Invalid required fields are rejected and do not create a destination record.

The pilot passes only if all six cases pass and the corresponding event and
destination states can be checked. These criteria test integration behavior;
they do not establish a business outcome or a service-level guarantee.

## Why a buyer might choose it — and why they might not

The value hypothesis is that a buyer may prefer a dedicated handoff layer if
their current path lacks a durable record across the form-to-CRM boundary,
duplicate-safe recovery, or a useful way to find failed deliveries. A
consistent, inspectable recovery path may be easier to operate than
reconciling separate tools manually. This advantage is conditional and has not
been demonstrated against a real workflow.

The opportunity may not be attractive if the CRM's native form handling or an
existing automation tool already delivers the required behavior reliably and
with less operational burden. It may also be unattractive if failures are
rare or inconsequential, the business has no owner for unresolved events, the
form cannot provide a reliable event identifier, the CRM cannot support the
needed create/update behavior, or the required security and data-handling
controls exceed what the service can provide.

No competitor comparison has been performed. The simplest adequate native or
ordinary automation option should be checked before proposing custom
infrastructure.

## Short demonstration narrative using Stage 8

Use the existing local demo and explain each result as a behavior under test,
not proof of an external integration:

1. **A valid enquiry is processed.** This shows the basic intake-to-contact
   path using the simulated CRM; it does not show a real website or CRM.
2. **The identical event is repeated.** The event and simulated contact counts
   stay unchanged, illustrating the intended protection against duplicate
   delivery.
3. **The simulated CRM write succeeds but its acknowledgement is lost.** The
   event is visibly pending retry. This illustrates an uncertain outcome; it
   does not reproduce a real CRM's network or failure behavior.
4. **The due retry runs after the demo clock advances.** The simulated contact
   count does not increase, illustrating recovery without a duplicate in this
   SQLite simulation. No real waiting or external retry occurs.
5. **A persistent simulated failure reaches the attempt limit.** The event is
   visibly terminal and counted for inspection. The prototype does not send an
   operator alert.

## Minimum validation checklist before further engineering investment

- [ ] **Verify the actual workflow:** observe or document how one target
  contractor's website enquiry currently reaches its CRM, who owns each step,
  and what happens when a handoff is delayed or fails.
- [ ] **Verify a real integration gap:** inspect the current form and CRM
  settings, available delivery/error records, and the native or ordinary
  automation options. Identify a specific failure or recovery behavior they do
  not already cover.
- [ ] **Verify frequency or consequence:** use the business's own available
  records to establish whether the failure occurs and what staff time,
  follow-up delay, or other consequence it causes. Record the source and
  observation period; do not substitute demo scenarios for customer evidence.
- [ ] **Verify buyer interest:** ask the owner/operator or responsible
  operations buyer whether they would consider a paid, narrowly scoped pilot
  for the verified gap and what proof they would require. Record an explicit
  next step, not just general enthusiasm. Do not imply a price or demand level
  before it is established.
- [ ] **Check feasibility and responsibility:** confirm the form can provide
  the required event data, the CRM supports the agreed record operation, and
  the business can authorize access and own unresolved exceptions.

This checklist is for future validation; no outreach or customer validation is
claimed as completed.

## Decision required before the next engineering stage

Make a **go/no-go decision on building one real form-to-CRM pilot**. Proceed
only if a target business confirms its current workflow, a specific material
integration gap is evidenced, the simplest native or automation alternatives
do not adequately address it, and the responsible buyer is willing to consider
a paid pilot. If any of those conditions is not met, do not invest in a real
CRM connector yet; revise the use-case hypothesis or stop this direction.
