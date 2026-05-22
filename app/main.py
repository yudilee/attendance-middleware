"""
Attendance Middleware — FastAPI Application

This is the main entry point that wires together all route modules.
Route-specific logic has been moved to app/api/v1/routes/*.py
"""
import sys
import os
import asyncio
import structlog

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text

# ── Rate Limiting (slowapi) ────────────────────────────────────────────
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ── App Modules ────────────────────────────────────────────────────────
from app.database.models import init_db, SessionLocal, AdminUser
from app.services.auth_ui import get_password_hash
from app.services.auth import verify_api_key
from app.cache import init_redis, close_redis
from app.config import settings
from app.services.adms_scraper import sync_employees_from_adms

# ── Structured Logging ─────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer() if settings.env == "development"
        else structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()

# ── ARQ Pool (global, initialized at startup) ──────────────────────────
arq_pool: Optional["arq.ArqRedis"] = None


async def adms_sync_loop():
    """Background task to sync ADMS employees daily at 2:00 AM."""
    while True:
        try:
            db = SessionLocal()
            try:
                success, msg = sync_employees_from_adms(db)
                if success:
                    logger.info("adms_sync_completed", result=msg)
                else:
                    logger.warning("adms_sync_failed", reason=msg)
            finally:
                db.close()
        except Exception as e:
            logger.error("adms_sync_error", exc_info=True, error=str(e))
        await asyncio.sleep(86400)


# ─── Lifespan ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global arq_pool
    logger.info("initializing_database")
    init_db()

    logger.info("initializing_redis")
    try:
        await init_redis()
        logger.info("redis_connected")
    except Exception as e:
        logger.warning("redis_connection_failed", error=str(e))

    # Auto-create default admin if none exists
    db = SessionLocal()
    try:
        admin = db.query(AdminUser).first()
        if not admin:
            logger.info("creating_default_admin")
            default_admin = AdminUser(
                username="admin",
                hashed_password=get_password_hash("admin"),
                role="superadmin",
            )
            db.add(default_admin)
            db.commit()
    finally:
        db.close()

    # Initialize ARQ pool
    import arq
    logger.info("initializing_arq_pool")
    try:
        redis_settings = arq.connections.RedisSettings(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
        )
        arq_pool = await arq.create_pool(redis_settings)
        logger.info("arq_pool_initialized")
        # Re-configure routes with the initialized arq_pool
        punch_routes.configure(arq_pool, limiter)
        admin_ui_routes.configure(arq_pool)
    except Exception as e:
        logger.warning("arq_pool_initialization_failed", error=str(e))
        arq_pool = None

    yield

    # Graceful shutdown
    logger.info("shutting_down_arq_pool")
    if arq_pool:
        arq_pool.close()
        await arq_pool.wait_closed()
    logger.info("stopping_redis")
    await close_redis()
    logger.info("adms_background_tasks_stopped")


app = FastAPI(title="Secure Geo-Fenced Attendance Aggregator", lifespan=lifespan)

# ── CORS Setup ─────────────────────────────────────────────────────────
cors_list = [origin.strip() for origin in settings.cors_origins.split(",")] if settings.cors_origins else []
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Rate Limiter Setup ─────────────────────────────────────────────────
from app.limiter import limiter

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Static Files & Templates ───────────────────────────────────────────
static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
app.mount("/static", StaticFiles(directory=static_path), name="static")

# ── Selfie Upload Directory ────────────────────────────────────────────
UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "uploads", "selfies",
)
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════
# ROUTE MODULES
# ═══════════════════════════════════════════════════════════════════════

from app.api.v1.routes import punch as punch_routes
from app.api.v1.routes import device as device_routes
from app.api.v1.routes import supervisor as supervisor_routes
from app.api.v1.routes import health as health_routes
from app.api.v1.routes import admin_ui as admin_ui_routes
from app.api.v1.routes import summary as summary_routes

# Configure shared ARQ pool and limiter references
punch_routes.configure(arq_pool, limiter)
device_routes.configure(limiter)
admin_ui_routes.configure(arq_pool)

# Include all route modules
app.include_router(punch_routes.router)
app.include_router(device_routes.router)
app.include_router(supervisor_routes.router)
app.include_router(health_routes.router)
app.include_router(admin_ui_routes.router)
app.include_router(summary_routes.router)
