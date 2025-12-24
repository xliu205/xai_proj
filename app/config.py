import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Settings:
    db_path: str = os.getenv("DB_PATH", "data/conversations.db")
    grok_api_key: Optional[str] = os.getenv("GROK_API_KEY")
    grok_base_url: str = os.getenv("GROK_BASE_URL", "https://api.x.ai/v1")
    grok_default_model: str = os.getenv("GROK_MODEL", "grok-3")
    inbound_rps: int = int(os.getenv("INBOUND_RPS", "100"))
    outbound_rps: int = int(os.getenv("OUTBOUND_RPS", "10"))
    batch_size: int = int(os.getenv("BATCH_SIZE", "10"))
    batch_flush_seconds: float = float(os.getenv("BATCH_FLUSH_SECONDS", "0.75"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "3"))
    backoff_seconds: float = float(os.getenv("BACKOFF_SECONDS", "1.5"))


settings = Settings()
