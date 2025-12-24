# xAI Conversation Insights Pipeline

Production-minded MVP that ingests conversations via an async FastAPI ingress, queues them for background processing, batches Grok calls under rate limits, and stores untrusted LLM output alongside raw payloads for review.

## System Overview
- **Ingress API (FastAPI)** — validates input, enforces 100 req/s token-bucket rate limit, immediately queues work, always returns `202 Accepted`.
- **Async processing worker** — drains the queue, batches payloads, and calls Grok with outbound limit of 10 calls/sec. Partial failures in a batch are isolated.
- **Storage (SQLite by default)** — conversations with status (`queued`, `processing`, `completed`, `failed`), raw payload, timestamps; insights table with Grok output + raw response blob. **LLM output is treated as untrusted input** and re-validated before persisting.
- **Reliability** — strict JSON prompts, pydantic schema validation, exponential backoff with `Retry-After` handling, fallback to failure state after retries instead of crashing.

### Architecture Diagram (ASCII)
```
[Client]
  | POST /api/v1/conversations (rate limited 100 rps)
  v
[FastAPI ingress] -> [SQLite: conversations(status=queued)] -> [asyncio.Queue]
                                                     ^
                                                     |
                           [Background worker batching <=BATCH_SIZE]
                           |  outbound limiter 10 calls/sec
                           v
                        [Grok API]
                           |
                           v
                 [SQLite: insights + raw Grok response]
```

### Data Flow
1) Client submits a conversation payload (ordered messages + optional metadata).
2) Request is rate limited; if accepted, the conversation is stored as `queued` and pushed into an in-memory queue (also reseeded on startup from the DB).
3) Worker drains queue in batches (size `BATCH_SIZE`, flush interval `BATCH_FLUSH_SECONDS`), marks items `processing`, and invokes Grok via a structured prompt.
4) Responses are validated; valid rows are written to `insights` and marked `completed`. Invalid/missing rows are marked `failed` with error text so the batch continues.
5) Insights are fetched via `/api/v1/insights` with time-window filters, limits, and optional confidence/sentiment filters; metadata includes total vs returned counts.

### Grok Prompt Strategy
- System prompt: position Grok as an internal insights engine; sentiment range enforced (-1.0 to 1.0); confidence reflects certainty, not sentiment.
- User prompt: provide enumerated conversation_id + text pairs and demand **strict JSON**: `{ "results": [ {"conversation_id": str, "sentiment_score": float, "clusters": [str], "confidence": float, "reasoning": str} ] }`.
- Response format: `response_format={"type": "json_object"}` with pydantic validation. Missing/invalid rows are isolated, not retried indefinitely.
- Retry policy: exponential backoff on malformed/HTTP errors, honor `Retry-After` on 429s, cap attempts at `MAX_RETRIES`; terminal failure marks rows as `failed`.

## API
### POST /api/v1/conversations (async ingress)
- Validates payload, enqueues, returns `202`:
```json
{
  "status": "accepted",
  "conversation_id": "conv_xxx",
  "message": "Conversation queued for analysis"
}
```
- Rate limited at 100 req/s; excess returns `429` + `Retry-After`.
- Never calls Grok synchronously.

### GET /api/v1/insights
Query params: `start_time` (ISO, required), `end_time` (required), `limit<=1000`, `min_confidence`, `sentiment` (`positive|neutral|negative`).
Returns insights with metadata (`total_count`, `returned_count`, `time_window`).

### Health
`GET /healthz` → `{ "status": "ok" }`

## Failure Modes & Mitigations
- **Inbound overload** → token-bucket 429 with retry-after.
- **Outbound Grok throttling (429)** → respect `Retry-After`; exponential backoff.
- **Malformed Grok JSON** → schema validation; retry capped; mark row `failed` without blocking batch.
- **Process crash/restart** → queue is reseeded from DB entries in `queued`/`processing` on startup.
- **LLM safety** → never trust raw output; stored as text blob + validated fields.

## Scaling Plan
- Swap SQLite for Postgres (same schema) for concurrent writers and horizontal API replicas.
- Move the queue to Redis/RQ or Kafka for multi-worker durability; keep the same worker contract.
- Increase batch size and parallel workers; outbound limiter already caps Grok calls/sec.
- Add observability hooks (Prometheus metrics for queue depth, Grok latency, failure rates).
- Harden idempotency: reject duplicate `conversation_id` or add versioning on reprocess.

## Running Locally
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export GROK_API_KEY=...  # optional; without it a deterministic heuristic runs offline
python3 main.py  # uvicorn entrypoint
```

Or Docker:
```bash
docker-compose up --build
```

Env vars:
- `DB_PATH` (default `data/conversations.db`)
- `GROK_API_KEY`, `GROK_MODEL`
- `INBOUND_RPS` (default 100), `OUTBOUND_RPS` (default 10)
- `BATCH_SIZE`, `BATCH_FLUSH_SECONDS`, `MAX_RETRIES`, `BACKOFF_SECONDS`

## What is intentionally not built
- No UI/frontend, embeddings/vector DBs, or topic modeling beyond cluster slugs.
- No synchronous Grok calls; everything is queued.
- No notebook-style scripts; interactions are via HTTP API.

## Files of interest
- `main.py`: FastAPI ingress + rate limiting + lifecycle hooks.
- `app/worker.py`: background batching worker + Grok calls + status transitions.
- `app/grok_client.py`: Grok integration with strict JSON, retries, and heuristic fallback.
- `app/db.py`: async SQLite schema + persistence.
- `app/rate_limiter.py`: token-bucket limiter used for both inbound/outbound.
