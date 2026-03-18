import asyncio, time
from dataclasses import dataclass
from typing import Any, Dict, Optional

@dataclass
class CacheEntry:
    value: Any
    expires_at: float
    version: int = 0  # на случай принудительной инкрементации

class MetaCache:
    """
    Простой in-memory кэш для метаданных (list/check/props).
    - TTL на уровне ключа
    - потокобезопасность через asyncio.Lock
    - префиксная инвалидация (удобно для директорий)
    """
    def __init__(self, max_items: int = 5000):
        self._data: Dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._max_items = max_items
        self._version = 0  # глобальный bump

    async def get(self, key: str) -> Optional[Any]:
        now = time.monotonic()
        async with self._lock:
            ce = self._data.get(key)
            if not ce or ce.expires_at < now:
                if ce:
                    self._data.pop(key, None)
                return None
            return ce.value

    async def set(self, key: str, value: Any, ttl: float):
        now = time.monotonic()
        async with self._lock:
            if len(self._data) >= self._max_items:
                # примитивная эвикция: выкинуть просроченные, если не помогло — половину
                expired = [k for k,v in self._data.items() if v.expires_at < now]
                for k in expired:
                    self._data.pop(k, None)
                if len(self._data) >= self._max_items:
                    # drop каждый второй (дешёво и сердито)
                    for i, k in enumerate(list(self._data.keys())):
                        if i % 2 == 0:
                            self._data.pop(k, None)
            self._data[key] = CacheEntry(value=value, expires_at=now + max(0.1, ttl), version=self._version)

    async def invalidate(self, key: str):
        async with self._lock:
            self._data.pop(key, None)

    async def invalidate_prefix(self, prefix: str):
        async with self._lock:
            for k in list(self._data.keys()):
                if k.startswith(prefix):
                    self._data.pop(k, None)

    async def bump(self):
        async with self._lock:
            self._version += 1

# глобальный экземпляр
meta_cache = MetaCache()
