import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import aiosqlite

from app.config import settings
from app.models import InsightsQuery, InsightPayload


async def get_connection() -> aiosqlite.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(settings.db_path)
    conn.row_factory = aiosqlite.Row
    return conn


async def init_db() -> None:
    async with await get_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT UNIQUE,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                sentiment_score REAL,
                clusters TEXT,
                confidence REAL,
                reasoning TEXT,
                model TEXT,
                raw_response TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.commit()


async def enqueue_conversation(conversation_id: str, payload: Dict[str, Any]) -> None:
    now = datetime.utcnow().isoformat()
    async with await get_connection() as conn:
        await conn.execute(
            """
            INSERT OR REPLACE INTO conversations (
                conversation_id, payload, status, error, created_at, updated_at
            ) VALUES (?, ?, 'queued', NULL, COALESCE((SELECT created_at FROM conversations WHERE conversation_id = ?), ?), ?)
            """,
            (conversation_id, json.dumps(payload), conversation_id, now, now),
        )
        await conn.commit()


async def mark_status(conversation_ids: Sequence[str], status: str, error: Optional[str] = None) -> None:
    if not conversation_ids:
        return
    now = datetime.utcnow().isoformat()
    async with await get_connection() as conn:
        await conn.executemany(
            """
            UPDATE conversations
            SET status = ?, error = ?, updated_at = ?
            WHERE conversation_id = ?
            """,
            [(status, error, now, cid) for cid in conversation_ids],
        )
        await conn.commit()


async def fetch_conversations(conversation_ids: Sequence[str]) -> List[aiosqlite.Row]:
    if not conversation_ids:
        return []
    placeholders = ",".join("?" for _ in conversation_ids)
    async with await get_connection() as conn:
        cur = await conn.execute(
            f"SELECT conversation_id, payload, status FROM conversations WHERE conversation_id IN ({placeholders})",
            tuple(conversation_ids),
        )
        rows = await cur.fetchall()
    return rows


async def load_outstanding(limit: int) -> List[str]:
    async with await get_connection() as conn:
        cur = await conn.execute(
            """
            SELECT conversation_id FROM conversations
            WHERE status IN ('queued', 'processing')
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def store_insights(insights: Iterable[InsightPayload]) -> None:
    async with await get_connection() as conn:
        await conn.executemany(
            """
            INSERT INTO insights (
                conversation_id, sentiment_score, clusters, confidence, reasoning, model, raw_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    insight.conversation_id,
                    insight.sentiment_score,
                    json.dumps(insight.clusters),
                    insight.confidence,
                    insight.reasoning,
                    insight.model,
                    json.dumps(insight.raw_response),
                )
                for insight in insights
            ],
        )
        await conn.commit()


async def query_insights(filters: InsightsQuery) -> Tuple[List[Dict[str, Any]], int]:
    clauses = ["created_at BETWEEN ? AND ?"]
    params: List[Any] = [filters.start_time.isoformat(), filters.end_time.isoformat()]
    if filters.min_confidence is not None:
        clauses.append("confidence >= ?")
        params.append(filters.min_confidence)
    if filters.sentiment:
        if filters.sentiment == "positive":
            clauses.append("sentiment_score > 0.2")
        elif filters.sentiment == "negative":
            clauses.append("sentiment_score < -0.2")
        elif filters.sentiment == "neutral":
            clauses.append("sentiment_score BETWEEN -0.2 AND 0.2")
    where = " AND ".join(clauses)
    async with await get_connection() as conn:
        data_cursor = await conn.execute(
            f"SELECT conversation_id, sentiment_score, clusters, confidence, reasoning, model, created_at FROM insights WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, filters.limit),
        )
        rows = await data_cursor.fetchall()

        count_cursor = await conn.execute(
            f"SELECT COUNT(1) FROM insights WHERE {where}",
            tuple(params),
        )
        total_count = (await count_cursor.fetchone())[0]

    results = []
    for row in rows:
        results.append(
            {
                "conversation_id": row["conversation_id"],
                "sentiment_score": row["sentiment_score"],
                "clusters": json.loads(row["clusters"] or "[]"),
                "confidence": row["confidence"],
                "reasoning": row["reasoning"],
                "model": row["model"],
                "created_at": row["created_at"],
            }
        )
    return results, total_count
