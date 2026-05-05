"""Redis cache client for attendance system."""
import json
import os
import redis.asyncio as aioredis
from typing import Optional, Any

redis_client: Optional[aioredis.Redis] = None

async def init_redis():
    global redis_client
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    redis_client = await aioredis.from_url(redis_url, decode_responses=True)
    return redis_client

async def close_redis():
    global redis_client
    if redis_client:
        await redis_client.close()
        redis_client = None

async def get_cache(key: str) -> Optional[str]:
    if redis_client is None:
        return None
    try:
        return await redis_client.get(key)
    except Exception:
        return None

async def set_cache(key: str, value: str, ttl: int = 300):
    if redis_client is None:
        return
    try:
        await redis_client.setex(key, ttl, value)
    except Exception:
        pass

async def invalidate_cache(pattern: str):
    """Invalidate cache keys matching a pattern (e.g., 'device_config:*')."""
    if redis_client is None:
        return
    try:
        cursor = 0
        while True:
            cursor, keys = await redis_client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                await redis_client.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass
