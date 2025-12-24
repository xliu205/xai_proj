import asyncio
import time
from typing import Tuple


class TokenBucket:
    def __init__(self, rate_per_sec: int, capacity: int | None = None):
        self.rate = rate_per_sec
        self.capacity = capacity or rate_per_sec
        self.tokens = float(self.capacity)
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated_at
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated_at = now

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait_for = (1 - self.tokens) / self.rate
            await asyncio.sleep(wait_for)

    async def try_acquire(self) -> Tuple[bool, float]:
        async with self._lock:
            self._refill()
            if self.tokens >= 1:
                self.tokens -= 1
                return True, 0.0
            retry_after = max((1 - self.tokens) / self.rate, 0.0)
            return False, retry_after
