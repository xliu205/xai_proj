import asyncio
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app import db
from app.config import settings
from app.models import ConversationIn, InsightsQuery
from app.rate_limiter import TokenBucket
from app.worker import ProcessingWorker


app = FastAPI(title="Backend Engineer Insights Platform", version="1.0.0")
inbound_limiter = TokenBucket(rate_per_sec=settings.inbound_rps)
processing_queue: asyncio.Queue[str] = asyncio.Queue()
worker = ProcessingWorker(queue=processing_queue)
worker_task: Optional[asyncio.Task] = None


@app.middleware("http")
async def enforce_rps(request: Request, call_next):
    allowed, retry_after = await inbound_limiter.try_acquire()
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"error": "Too Many Requests"},
            headers={"Retry-After": f"{retry_after:.2f}"},
        )
    return await call_next(request)


@app.on_event("startup")
async def startup_event() -> None:
    await db.init_db()
    global worker_task
    worker_task = asyncio.create_task(worker.start())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if worker_task:
        worker_task.cancel()
    await worker.shutdown()


@app.post("/api/v1/conversations", status_code=202)
async def create_conversation(payload: ConversationIn):
    conversation_id = payload.conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
    await db.enqueue_conversation(conversation_id, payload.dict())
    await processing_queue.put(conversation_id)
    return {
        "status": "accepted",
        "conversation_id": conversation_id,
        "message": "Conversation queued for analysis",
    }


async def query_params(
    start_time: str,
    end_time: str,
    limit: int = 100,
    min_confidence: Optional[float] = None,
    sentiment: Optional[str] = None,
) -> InsightsQuery:
    try:
        return InsightsQuery(
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            min_confidence=min_confidence,
            sentiment=sentiment,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/v1/insights")
async def list_insights(filters: InsightsQuery = Depends(query_params)):
    insights, total_count = await db.query_insights(filters)
    return {
        "metadata": {
            "total_count": total_count,
            "returned_count": len(insights),
            "time_window": {
                "start": filters.start_time,
                "end": filters.end_time,
            },
        },
        "data": insights,
    }


@app.get("/healthz")
async def healthcheck():
    return {"status": "ok"}


def get_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
