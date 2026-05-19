"""Health check and monitoring endpoints."""
import structlog
from datetime import datetime

from fastapi import APIRouter
from sqlalchemy import text

from app.database.models import SessionLocal
from app.services.adms_service import _handshake_state

logger = structlog.get_logger()

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check():
    """Health check endpoint for container orchestration and monitoring."""
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db_ok = True
        db.close()
    except Exception:
        pass

    adms_ok = _handshake_state.get("handshake_done", False)

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "adms": "connected" if adms_ok else "disconnected",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
    }
