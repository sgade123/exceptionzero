# ExceptionZero

**A six-agent fleet that resolves enterprise workflow exceptions — and stops when it shouldn't touch one.**

Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/) · Track: **Fortified Enterprise Fleet**

---

Every business has someone who opens a spreadsheet of failed payments every Monday and works it by hand: pull the invoice, check the customer record, look at what happened last time, decide what went wrong, fix it, confirm it worked.

An exception is by definition the case automation *couldn't* handle. If a rule could resolve it, it wouldn't be an exception. That's why this work has never been automated — it needs judgment under incomplete information, with money and regulatory exposure attached.

ExceptionZero automates the judgment, and — more importantly — knows when to hand back.

**Demo:** payment exceptions for a 40-person industrial distributor. **The engine is domain-neutral:** nothing in the six agents knows what a payment is. They know what an exception is.

---

## Architecture

![Architecture](architecture.svg)

### The permission model is the design

Each agent runs under its own Google Cloud service account with the narrowest role set that lets it work:

| Agent | Service account | Can do |
|---|---|---|
| Triage | `ez-triage@` | read the exception queue |
| Context Coordinator | `ez-coord@` | dispatch specialists |
| Invoice / Counterparty / History / Precedent | `ez-invoice@` `ez-customer@` `ez-history@` `ez-precedent@` | read exactly one table each |
| **Diagnosis** | `ez-diagnosis@` | **nothing but call Vertex** |
| Execution | `ez-exec@` | write to `exceptions` — and nothing else |
| Verification | `ez-verify@` | read + trigger rollback |

Verify the central claim yourself:

```bash
gcloud projects get-iam-policy $PROJECT \
  --flatten="bindings[].members" \
  --filter="bindings.members:ez-diagnosis" \
  --format="table(bindings.role)"
```

Two roles come back: `aiplatform.user` and `cloudtrace.agent`. No BigQuery, so the Diagnosis agent cannot start a query. No `dataViewer`, so it could not read a result if it had one. It reasons over evidence another agent retrieved, and Google Cloud enforces that — not a prompt, not a Python convention.

### What the model is allowed to contribute

A proposed action, a rationale, evidence citations, and a confidence score. Everything else is the system's:

- **Reversibility** is derived from the compensating-action table, not the model's self-report. Otherwise a confidently wrong agent could widen its own authority.
- **The act/escalate decision** is deterministic code. A probabilistic gate is not a control.
- **Evidence** comes only from the Context agent. A resolution citing anything else is rejected mechanically.

### Guardrails

| Failure | Mitigation |
|---|---|
| Agent loops | 4-turn cap; orchestrator kills, case to a human, trace preserved |
| Hallucinated resolution | Cited evidence IDs checked against what was actually retrieved; entities named in the rationale checked against cited records |
| Malformed handoff | Pydantic schema validation on every inter-agent message |
| Double execution | Idempotency key per exception; replay is a no-op |
| Bad execution | Compensating action written *before* the act; verification rolls back |
| Systemic failure | Circuit breaker halts the fleet after 3 verification failures in 20 cases |
| Prompt injection | Model Armor + triage instruction treating untrusted text as data → `quarantine` |

---

## Run it

### Prerequisites

Python 3.10+, `gcloud` authenticated, a Google Cloud project with billing enabled.

```bash
gcloud auth application-default login   # required — the SDK reads these, not the CLI creds
gcloud config set project YOUR_PROJECT_ID
gcloud services enable aiplatform.googleapis.com run.googleapis.com \
  cloudbuild.googleapis.com pubsub.googleapis.com \
  firestore.googleapis.com bigquery.googleapis.com
```

### 1. Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Generate the data estate

Synthetic, seeded, byte-identical on every run:

```bash
python generate_dataset.py                      # local JSONL
python generate_dataset.py --bq YOUR_PROJECT_ID # + load to BigQuery
```

339 exceptions across 8 types, 700 invoices, 60 customers, and the corroborating payment history that makes each exception genuinely resolvable — plus three engineered cases:

- `EXC-799001` — a large unexplained shortfall the fleet must refuse
- `EXC-799002` — a prompt injection in the payment memo field
- `EXC-799003` — a reference to an invoice that does not exist

### 3. Create the agent identities

```bash
./setup_iam.sh YOUR_PROJECT_ID
```

Creates nine service accounts and grants least-privilege roles. Idempotent; re-run it after loading BigQuery so the per-table grants land.

### 4. Run the fleet

```bash
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
export GOOGLE_CLOUD_LOCATION=global
export EZ_MODEL=gemini-3.5-flash
export EZ_MODEL_REASONING=gemini-3.5-flash

STUB=1 python orchestrator.py --limit 339 --quiet            # no model calls, instant
STUB=0 python orchestrator.py --limit 20 --workers 8 --quiet # real Gemini
```

`STUB=1` runs deterministic fake agents — useful for exercising the guards without cost or latency. `STUB=0` runs the real fleet.

### 5. Break it on purpose

The rubric asks how the system recovers when a worker agent loops or hallucinates. A well-behaved model can't demonstrate that, so the faults are injected deliberately:

```bash
STUB=1 python orchestrator.py --limit 30 --inject hallucination  # cites evidence it never got
STUB=1 python orchestrator.py --limit 30 --inject phantom_key    # real EV-id, phantom entity
STUB=1 python orchestrator.py --limit 30 --inject loop           # never terminates
STUB=1 python orchestrator.py --limit 30 --inject verify_fail    # trips the circuit breaker
STUB=1 python orchestrator.py --limit 30 --inject overconfident  # forces confidence to 1.0
```

Only the injected agent changes. Every guard stays exactly as it ships.

### 6. Deploy

```bash
./deploy.sh YOUR_PROJECT_ID
```

Builds the container, deploys to Cloud Run, creates the Pub/Sub topic and push subscription. Prints the service URL.

```bash
curl $URL/healthz
gcloud pubsub topics publish exceptions --message '{"exception_id":"EXC-799001"}'
gcloud beta run services logs tail exceptionzero --region us-central1
```

| Endpoint | Purpose |
|---|---|
| `GET /` | live trace viewer — run the fleet, inject faults, see agent scopes |
| `POST /run` | batch execution; returns outcomes, spans, registry |
| `POST /pubsub` | Pub/Sub push handler — one exception per message |
| `GET /healthz` | liveness, mode, model |

---

## Results

A representative 20-case run against real Gemini 3.5 Flash:

```
deferred=11  quarantined=1  resolved=8
20 cases in 43.5s (2.18s/case, 8 workers)
```

Roughly 40% resolved autonomously. **Every escalation carries a stated reason**, and most are correct by design — an invalid account number or a sanctions hit should never be auto-resolved, because fixing it requires contacting a human being.

The refusal, in the fleet's own words:

> The payment of EUR 44,704.73 is significantly less than the invoice amount of EUR 81,281.32 for INV-20232, leaving an unexplained shortfall of EUR 36,576.59.

---

## Findings

**A rigorous reasoner is a data-quality test.** Stub agents resolved 40% of cases because the type-to-action mapping was hardcoded. Real Gemini resolved *zero* — and was right to. Every escalation pointed at a flaw in the synthetic estate: duplicate submissions with no original payment on file, invoices marked open that had already settled twice. The model refused to assert what the evidence didn't support. Each jump in resolution rate came from fixing the data, never from loosening the agents.

**Reversibility can't be self-reported.** Early on, the Diagnosis agent decided that voiding a duplicate was irreversible and blocked its own resolutions. The fix wasn't a better prompt — it was removing the question from the model entirely. An action is reversible iff a compensating action is defined for it. Letting an agent describe the consequences of its own proposal is the same failure the gate exists to prevent.

**Confidence needs anchors or it saturates.** The first real runs returned 1.00 on every case, including a 45% unexplained shortfall. Numeric anchors plus a required `unexplained` field — where anything listed forces confidence below the floor — produced calibrated scores that the gate can actually use.

**Well-behaved models make bad demos.** The planted hallucination bait never fired, because the model correctly reported the invoice as absent instead of fabricating. Deliberate fault injection turned out to be both more honest and more convincing than waiting for a failure that shouldn't happen.

---

## Stack

Gemini 3.5 Flash via Vertex AI · Google ADK · Cloud Run · Pub/Sub · Firestore · BigQuery · Model Armor · Cloud Trace · Python 3.12 · FastAPI · Pydantic

## Files

| | |
|---|---|
| `orchestrator.py` | Agent Gateway, registry, tracing, guard enforcement |
| `fleet_core.py` | Handoff contracts, citation guards, risk gate, loop guard, circuit breaker |
| `fleet.py` | Scoped tool functions and agent instructions |
| `agents_real.py` | Gemini-backed handlers |
| `faults.py` | Deliberate fault injection |
| `generate_dataset.py` | Seeded synthetic data estate |
| `setup_iam.sh` | Per-agent service accounts and least-privilege roles |
| `service.py` | Cloud Run service and trace viewer |
| `deploy.sh` | Build, deploy, wire Pub/Sub |
