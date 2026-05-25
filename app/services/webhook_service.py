"""
Asynchronous webhook dispatcher service with HMAC signatures (Phase 5F).
"""
import os
import structlog
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database.models import Webhook

logger = structlog.get_logger()

async def fire_webhook(db: Session, event: str, payload: dict, arq_pool=None):
    """
    Triggers a webhook event by finding subscribed active webhooks 
    and dispatching them asynchronously via ARQ background tasks.
    """
    try:
        # Query active webhooks
        webhooks = db.query(Webhook).filter(Webhook.is_active == True).all()
        
        subscribed_webhooks = []
        for wh in webhooks:
            if not wh.events:
                continue
            events_list = [e.strip() for e in wh.events.split(",") if e.strip()]
            if "*" in events_list or event in events_list:
                subscribed_webhooks.append(wh)
                
        if not subscribed_webhooks:
            return
            
        logger.info("webhook_triggered", event=event, matching_subscribers=len(subscribed_webhooks))
        
        # Resolve/Create ARQ Pool if not passed
        pool = arq_pool
        created_pool = False
        if pool is None:
            try:
                import arq
                from arq.connections import RedisSettings
                redis_settings = RedisSettings(
                    host=os.getenv("REDIS_HOST", "redis"),
                    port=int(os.getenv("REDIS_PORT", "6379")),
                    database=int(os.getenv("REDIS_DB", "0"))
                )
                pool = await arq.create_pool(redis_settings)
                created_pool = True
            except Exception as e:
                logger.error("webhook_redis_pool_failed", error=str(e))
                # Fallback to sync delivery / error log if arq pool is unavailable
                return
                
        for wh in subscribed_webhooks:
            await pool.enqueue_job("deliver_webhook", wh.id, event, payload)
            
        if created_pool:
            await pool.close()
            
    except Exception as e:
        logger.error("webhook_trigger_error", event=event, error=str(e))
