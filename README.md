# Backend Engineer Insights Platform

Loom video Demo： https://www.loom.com/share/f4c0844a0dc045cebeb3688ab43d7132

Production-minded MVP for an internal insights platform. It ingests support conversations, queues them for async Grok analysis under strict rate limits, and stores both raw payloads and validated LLM output for downstream consumers.

## System Overview
- **Ingress API (FastAPI)**: validates payloads, token-bucket rate limits at 100 req/s, persists immediately, enqueues, always returns `202 Accepted`. No synchronous Grok calls.
- **Async worker**: drains queue in batches, respects outbound Grok cap of 10 calls/sec, isolates per-item failures.
- **Storage (SQLite default)**: conversations with status (`queued`, `processing`, `completed`, `failed`), raw payload, timestamps; insights table with validated fields plus raw Grok response blob. **LLM output is treated as untrusted input**.

### Architecture Diagram (ASCII)
```
[Client]
  | POST /api/v1/conversations (100 rps limit)
  v
[FastAPI ingress] -> [SQLite: conversations(status=queued)] -> [asyncio.Queue]
                                                     ^
                                                     |
                           [Background worker batching <= BATCH_SIZE]
                           | outbound limiter 10 calls/sec
                           v
                        [Grok API]
                           |
                           v
                 [SQLite: insights + raw Grok response]
```

## Data Flow
1) Client sends ordered messages (+ optional metadata).
2) Request is validated and rate-limited; payload stored as `queued` and pushed to in-memory queue.
3) On startup, worker reseeds queue from `queued`/`processing` rows to survive crashes.
4) Worker drains queue in batches (`BATCH_SIZE`, `BATCH_FLUSH_SECONDS`), marks `processing`, and calls Grok with outbound cap.
5) Each result is validated independently. Valid rows → `insights` + status `completed`; invalid/missing → status `failed` with error text.
6) `/api/v1/insights` provides windowed access with filters (time, sentiment bucket, confidence, limit).

## Grok Prompt Strategy
Grok is used as a semantic enrichment layer; sentiment is clamped to [-1.0, 1.0], confidence reflects certainty (not polarity), ambiguity should lower confidence.

Structured response contract:
```json
{
  "results": [
    {
      "conversation_id": "string",
      "sentiment_score": 0.0,
      "clusters": ["string"],
      "confidence": 0.0,
      "reasoning": "string"
    }
  ]
}
```
- `response_format={"type": "json_object"}` enforced.
- Pydantic validation on receipt; malformed outputs retry with exponential backoff and honor `Retry-After` on 429. Retries are capped; terminal failures are recorded, not retried forever.
- Raw Grok response is persisted for auditability; validated fields are stored separately.

## API
### POST /api/v1/conversations
- Async ingress; validates and enqueues, returns immediately:
```json
{
  "status": "accepted",
  "conversation_id": "conv_xxx",
  "message": "Conversation queued for analysis"
}
```
- Rate limited to 100 req/s (429 + Retry-After if exceeded).
- Never calls Grok synchronously.

### GET /api/v1/insights
- Query params: `start_time` (ISO, required), `end_time` (required), `limit` (<=1000), `min_confidence` (optional), `sentiment` (`positive|neutral|negative`).
- Response includes metadata (`total_count`, `returned_count`, `time_window`) and insight rows.

### Health
- `GET /healthz` → `{ "status": "ok" }`.

## Failure Modes & Mitigations
- **Inbound overload**: token-bucket with Retry-After.
- **Outbound Grok throttling (429)**: outbound limiter + backoff honoring Retry-After.
- **Malformed/partial Grok output**: strict schema validation; per-row failure isolation.
- **Crash/restart**: queue reseeded from persisted `queued`/`processing`.
- **LLM safety**: raw output stored separately; validated before use.

## Scaling Plan
- Swap SQLite for Postgres for concurrent writers and horizontal API replicas.
- Move queue to Redis/RQ or Kafka for multi-worker durability.
- Increase batch size/workers; outbound limiter still caps calls/sec.
- Add observability (queue depth, Grok latency, failure rates).
- Harden idempotency with strict conversation_id rules or versioned reprocessing.

## Running Locally
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GROK_API_KEY=... 
python3 main.py  # uvicorn entrypoint
```

Docker:
```bash
docker-compose up --build
```

Key env vars:
- `DB_PATH` (default `data/conversations.db`)
- `GROK_API_KEY`, `GROK_MODEL`
- `INBOUND_RPS` (default 100), `OUTBOUND_RPS` (default 10)
- `BATCH_SIZE`, `BATCH_FLUSH_SECONDS`, `MAX_RETRIES`, `BACKOFF_SECONDS`

## Non-Goals
- No UI/dashboard
- No embeddings or vector DB
- No synchronous LLM calls
- No notebook-style workflows
