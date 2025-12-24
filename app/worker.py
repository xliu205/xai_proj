import asyncio
import json
from typing import Dict, List

from app import db
from app.config import settings
from app.grok_client import GrokClient
from app.models import InsightPayload


class ProcessingWorker:
    def __init__(self, queue: asyncio.Queue[str]):
        self.queue = queue
        self._stop_event = asyncio.Event()
        self._client = GrokClient()

    async def start(self) -> None:
        await db.init_db()
        outstanding = await db.load_outstanding(limit=500)
        for cid in outstanding:
            await self.queue.put(cid)
        while not self._stop_event.is_set():
            await self._drain_once()

    async def shutdown(self) -> None:
        self._stop_event.set()
        await self._client.close()

    async def _drain_once(self) -> None:
        batch: List[str] = []
        try:
            cid = await asyncio.wait_for(self.queue.get(), timeout=settings.batch_flush_seconds)
            batch.append(cid)
        except asyncio.TimeoutError:
            await asyncio.sleep(0)

        while len(batch) < settings.batch_size:
            try:
                batch.append(await asyncio.wait_for(self.queue.get(), timeout=0.05))
            except asyncio.TimeoutError:
                break

        if not batch:
            await asyncio.sleep(0.05)
            return

        rows = await db.fetch_conversations(batch)
        ready = [row for row in rows if row["status"] == "queued"]
        ready_ids = [row["conversation_id"] for row in ready]
        if not ready_ids:
            return
        await db.mark_status(ready_ids, status="processing")

        payloads = [
            {
                "conversation_id": row["conversation_id"],
                "text": _conversation_text(json.loads(row["payload"])),
            }
            for row in ready
        ]

        results = await self._client.analyze_batch(payloads, model=settings.grok_default_model)

        successes: List[InsightPayload] = []
        failures: Dict[str, str] = {}
        for cid, result in results.items():
            if isinstance(result, InsightPayload):
                successes.append(result)
            else:
                failures[cid] = str(result)

        if successes:
            await db.store_insights(successes)
            await db.mark_status([i.conversation_id for i in successes], status="completed")
        if failures:
            await db.mark_status(list(failures.keys()), status="failed", error="; ".join(failures.values()))


def _conversation_text(payload: Dict[str, object]) -> str:
    messages = payload.get("messages") or []
    if isinstance(messages, list):
        return "\n".join([
            f"{msg.get('author_id', 'user')}: {msg.get('text', '')}" if isinstance(msg, dict) else str(msg)
            for msg in messages
        ])
    return str(payload)
