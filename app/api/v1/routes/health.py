"""Health check and monitoring endpoints."""
import os
import shutil
import structlog
from datetime import datetime
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.database.models import SessionLocal, ADMSTarget
from app.services.adms_service import _handshake_state

logger = structlog.get_logger()

router = APIRouter(tags=["Health"])

def get_disk_free_gb() -> float:
    """Get free disk space in gigabytes for the uploads directory."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))))
    uploads_dir = os.path.join(backend_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    try:
        total, used, free = shutil.disk_usage(uploads_dir)
        return round(free / (2**30), 2)
    except Exception as e:
        logger.warning("failed_to_check_disk_space", error=str(e))
        return 0.0

@router.get("/health")
async def health_check():
    """Detailed health check endpoint for monitoring systems."""
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db_ok = True
        db.close()
    except Exception as e:
        logger.error("health_check_db_failed", error=str(e))

    redis_ok = False
    try:
        from app.cache import redis_client
        if redis_client:
            await redis_client.ping()
            redis_ok = True
    except Exception as e:
        logger.error("health_check_redis_failed", error=str(e))

    # Check ADMS Configuration Serials
    adms_configured = False
    try:
        db = SessionLocal()
        target = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
        adms_configured = bool(target and target.server_url)
        db.close()
    except Exception:
        pass

    adms_connected = _handshake_state.get("handshake_done", False)
    free_space_gb = get_disk_free_gb()
    disk_ok = free_space_gb > 0.5  # Warn if less than 500MB

    overall_status = "healthy"
    if not db_ok or not redis_ok:
        overall_status = "unhealthy"
    elif not disk_ok or (adms_configured and not adms_connected):
        overall_status = "degraded"

    return {
        "status": overall_status,
        "database": "connected" if db_ok else "disconnected",
        "redis": "connected" if redis_ok else "disconnected",
        "disk_space": {
            "status": "ok" if disk_ok else "low",
            "free_gb": free_space_gb
        },
        "adms": {
            "configured": adms_configured,
            "connected": adms_connected,
            "status": "connected" if adms_connected else ("unreachable" if adms_configured else "not_configured")
        },
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
    }

@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness_check():
    """Liveness probe: verifies that the application process is running."""
    return {"status": "alive", "timestamp": datetime.utcnow().isoformat()}

@router.get("/health/ready")
async def readiness_check(response: Response):
    """Readiness probe: verifies that backing database and cache services are ready."""
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db_ok = True
        db.close()
    except Exception:
        pass

    redis_ok = False
    try:
        from app.cache import redis_client
        if redis_client:
            await redis_client.ping()
            redis_ok = True
    except Exception:
        pass

    if db_ok and redis_ok:
        return {"status": "ready", "timestamp": datetime.utcnow().isoformat()}
    
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "not_ready",
        "database": "ready" if db_ok else "not_ready",
        "redis": "ready" if redis_ok else "not_ready",
        "timestamp": datetime.utcnow().isoformat()
    }
