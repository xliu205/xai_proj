from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Dict, List, Optional

import httpx
from pydantic import BaseModel, ValidationError, root_validator

from app.config import settings
from app.models import InsightPayload
from app.rate_limiter import TokenBucket


class GrokInsight(BaseModel):
    conversation_id: str
    sentiment_score: float
    clusters: List[str]
    confidence: float
    reasoning: str

    @root_validator(skip_on_failure=True)
    def clamp_values(cls, values):
        score = values.get("sentiment_score")
        if score is not None:
            values["sentiment_score"] = max(-1.0, min(1.0, score))
        conf = values.get("confidence")
        if conf is not None:
            values["confidence"] = max(0.0, min(1.0, conf))
        clusters = values.get("clusters") or []
        values["clusters"] = [str(c) for c in clusters][:10]
        return values


class GrokBatchResponse(BaseModel):
    results: List[GrokInsight]


class GrokClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = settings.grok_base_url,
        default_model: str = settings.grok_default_model,
        max_retries: int = settings.max_retries,
        backoff_seconds: float = settings.backoff_seconds,
        rate_limiter: Optional[TokenBucket] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._client = httpx.AsyncClient(timeout=30)
        self._rate_limiter = rate_limiter or TokenBucket(rate_per_sec=settings.outbound_rps)

    async def close(self) -> None:
        await self._client.aclose()

    async def analyze_batch(self, batch: List[Dict[str, str]], model: Optional[str] = None) -> Dict[str, InsightPayload | str]:
        if not batch:
            return {}
        model_id = model or self.default_model
        if not self.api_key:
            return {
                item["conversation_id"]: self._offline_heuristic(item["text"], model_id, item["conversation_id"])
                for item in batch
            }
        await self._rate_limiter.acquire()
        try:
            grok_results = await self._call_api(batch, model_id)
            parsed: Dict[str, InsightPayload | str] = {}
            for result in grok_results:
                parsed[result.conversation_id] = InsightPayload(
                    conversation_id=result.conversation_id,
                    sentiment_score=result.sentiment_score,
                    clusters=result.clusters,
                    confidence=result.confidence,
                    reasoning=result.reasoning,
                    model=model_id,
                    raw_response=result.dict(),
                )
            # Mark missing entries as failures without blocking batch
            missing = {item["conversation_id"] for item in batch} - set(parsed.keys())
            for cid in missing:
                parsed[cid] = "No response from Grok for conversation"
            return parsed
        except Exception as exc:  # noqa: BLE001
            return {item["conversation_id"]: f"grok_error: {exc}" for item in batch}

    async def analyze(self, text: str, model: Optional[str] = None, conversation_id: Optional[str] = None) -> InsightPayload:
        cid = conversation_id or "preview"
        results = await self.analyze_batch([
            {"conversation_id": cid, "text": text}
        ], model=model)
        output = results.get(cid)
        if isinstance(output, InsightPayload):
            return output
        raise RuntimeError(output or "Unknown Grok error")

    async def _call_api(self, batch: List[Dict[str, str]], model: str) -> List[GrokInsight]:
        url = f"{self.base_url}/chat/completions"
        formatted_conversations = "\n".join(
            [f"- id: {item['conversation_id']}, text: {item['text']}" for item in batch]
        )
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an internal conversation insights engine. "
                        "Return strict JSON with sentiment_score (-1.0 to 1.0), clusters (slug strings), confidence (0 to 1) representing certainty, and a short reasoning. "
                        "If input is ambiguous, lower the confidence."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Analyze the following conversations and respond ONLY with minified JSON matching this schema: {\"results\": [ {\"conversation_id\": str, \"sentiment_score\": float, \"clusters\": [str], \"confidence\": float, \"reasoning\": str} ] }. "
                        "Use the provided conversation_id. Do not hallucinate ids. Sentiment range -1.0 to 1.0."
                        f"\n\n{formatted_conversations}"
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                raw = resp.json()
                content = raw.get("choices", [{}])[0].get("message", {}).get("content")
                if not content:
                    raise ValueError("Empty Grok response")
                data = json.loads(content)
                parsed = GrokBatchResponse.parse_obj(data)
                return parsed.results
            except (httpx.HTTPError, json.JSONDecodeError, ValidationError, ValueError) as exc:
                if attempt == self.max_retries:
                    raise exc
                sleep_for = self.backoff_seconds * (2 ** (attempt - 1))
                await asyncio.sleep(sleep_for)
        raise RuntimeError("Unexpected retry exhaustion")

    def _offline_heuristic(self, text: str, model: str, conversation_id: str) -> InsightPayload:
        text_lower = text.lower()
        sentiment_words = {
            "positive": ["love", "great", "thanks", "smooth", "fast"],
            "negative": ["delay", "crash", "ignored", "disappointed", "help", "problem", "issue", "unresolved"],
        }
        score = 0.0
        for word in sentiment_words["positive"]:
            if word in text_lower:
                score += 0.25
        for word in sentiment_words["negative"]:
            if word in text_lower:
                score -= 0.3
        score = max(-1.0, min(1.0, score))

        clusters = []
        if "crash" in text_lower or "bug" in text_lower:
            clusters.append("app_stability")
        if "refund" in text_lower or "policy" in text_lower:
            clusters.append("policy_questions")
        if "delay" in text_lower or "shipping" in text_lower or "package" in text_lower:
            clusters.append("delivery_issues")
        if "love" in text_lower or "great" in text_lower:
            clusters.append("praise")
        if not clusters:
            clusters.append("general_support")

        if "?" in text or "anyone" in text_lower or text_lower.startswith("where"):
            clusters.append("knowledge_gap")

        confidence = 0.45 + 0.2 * random.random()
        reasoning = f"Heuristic {model} inference based on keywords and hashtags."

        return InsightPayload(
            conversation_id=conversation_id,
            sentiment_score=score,
            clusters=clusters,
            confidence=round(confidence, 2),
            reasoning=reasoning,
            model=model,
            raw_response={"source": "heuristic"},
        )
