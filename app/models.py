from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, root_validator, validator


class Message(BaseModel):
    author_id: Optional[str] = Field(None, description="External user identifier")
    text: str = Field(..., min_length=1)
    timestamp: Optional[datetime] = None
    inbound: Optional[bool] = None


class ConversationIn(BaseModel):
    conversation_id: Optional[str] = Field(None, description="Caller-provided id; if omitted server generates one")
    messages: List[Message]
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @validator("messages")
    def validate_messages(cls, value: List[Message]) -> List[Message]:
        if not value:
            raise ValueError("messages cannot be empty")
        return value

    @property
    def combined_text(self) -> str:
        # Preserve message order; lightweight delimiter for Grok context
        return "\n".join([msg.text.strip() for msg in self.messages if msg.text.strip()])


class ConversationRecord(BaseModel):
    conversation_id: str
    payload: Dict[str, Any]
    status: str
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class InsightPayload(BaseModel):
    conversation_id: str
    sentiment_score: float
    clusters: List[str]
    confidence: float
    reasoning: str
    model: str
    raw_response: Dict[str, Any]

    @root_validator(skip_on_failure=True)
    def clamp_values(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        score = values.get("sentiment_score")
        if score is not None:
            values["sentiment_score"] = max(-1.0, min(1.0, score))
        conf = values.get("confidence")
        if conf is not None:
            values["confidence"] = max(0.0, min(1.0, conf))
        return values


class InsightResponse(BaseModel):
    conversation_id: str
    sentiment_score: float
    clusters: List[str]
    confidence: float
    reasoning: str


class InsightsQuery(BaseModel):
    start_time: datetime
    end_time: datetime
    limit: int = Field(100, gt=0, le=1000)
    min_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    sentiment: Optional[str] = Field(
        None, description="Filter sentiment bucket: positive|neutral|negative"
    )

    @validator("end_time")
    def validate_range(cls, v: datetime, values: Dict[str, Any]) -> datetime:
        start = values.get("start_time")
        if start and v <= start:
            raise ValueError("end_time must be after start_time")
        return v

    @validator("sentiment")
    def validate_sentiment(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"positive", "neutral", "negative"}
        if v not in allowed:
            raise ValueError(f"sentiment must be one of {', '.join(sorted(allowed))}")
        return v
