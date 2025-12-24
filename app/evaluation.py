import asyncio
from dataclasses import dataclass
from typing import Dict, List

from app.grok_client import GrokClient


@dataclass
class EvalExample:
    text: str
    expected_sentiment: int  # -1, 0, 1


EVAL_SET: List[EvalExample] = [
    EvalExample("Love the latest features! Smooth and fast.", 1),
    EvalExample("The app keeps crashing when I open settings.", -1),
    EvalExample("Where can I find the refund policy?", 0),
    EvalExample("My ticket has been ignored for days.", -1),
    EvalExample("Thanks for the quick help!", 1),
]


def sentiment_bucket(score: float) -> int:
    if score > 0.2:
        return 1
    if score < -0.2:
        return -1
    return 0


async def evaluate_models(models: List[str]) -> Dict[str, float]:
    results: Dict[str, float] = {}
    for model in models:
        client = GrokClient(default_model=model)
        correct = 0
        for example in EVAL_SET:
            insight = await client.analyze(example.text, model=model)
            if sentiment_bucket(insight.sentiment_score) == example.expected_sentiment:
                correct += 1
        await client.close()
        results[model] = correct / len(EVAL_SET)
    return results
