"""Punch-related API v1 routes: single punch, batch punch, punch types, history, CSV export."""
import io
import json
import structlog
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from app.database.models import (
    SessionLocal, PunchLog, Employee, PunchType,
)
from app.services.auth import verify_api_key
from app.api.v1.schemas import (
    PunchRequest, PunchResponse, BatchPunchRequest,
    BatchPunchResponse, BatchPunchResult, PunchTypeResponse,
)
from app.services.punch_service import validate_and_prepare_punch, create_punch_log
from app.cache import get_cache, set_cache
from app.config import settings
from slowapi import Limiter
from slowapi.util import get_remote_address

logger = structlog.get_logger()

# ARQ pool reference (set during app lifespan)
arq_pool = None

router = APIRouter(tags=["Punch"])

from app.limiter import limiter

def configure(arq_pool_ref, limiter_ref):
    """Set the ARQ pool reference from the main app."""
    global arq_pool
    arq_pool = arq_pool_ref

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/api/v1/punch", response_model=PunchResponse)
async def create_punch(
    request: Request,
    punch_req: PunchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(verify_api_key),
):
    """Record a single attendance punch with full validation."""
    try:
        data = validate_and_prepare_punch(db, punch_req)
    except Exception as e:
        if hasattr(e, 'message'):
            raise HTTPException(
                status_code=getattr(e, 'status_code', 400),
                detail=e.message,
            )
        raise

    log = create_punch_log(db, data)

    # Enqueue ADMS sync via ARQ worker
    if arq_pool:
        try:
            await arq_pool.enqueue_job("sync_punches_to_adms", log.id)
        except Exception as e:
            logger.warning("arq_enqueue_failed", error=str(e))

    logger.info("punch_submitted",
                employee_id=log.employee_id,
                punch_type=log.punch_type,
                log_id=log.id)

    return {
        "status": "success",
        "message": f"Punch recorded: {punch_req.punch_type}",
        "server_time": log.timestamp,
        "log_id": log.id,
        "distance_meters": data.get("distance"),
        "branch_name": data.get("best_branch"),
        "in_fence": data.get("in_fence"),
    }


@router.post("/api/v1/punch/batch", response_model=BatchPunchResponse)
async def create_batch_punch(
    request: Request,
    batch_req: BatchPunchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(verify_api_key),
):
    """Accept up to 50 offline punches in one request for efficient sync."""
    if len(batch_req.punches) > 50:
        raise HTTPException(status_code=400, detail="Batch size limit is 50 punches.")

    results = []
    synced = 0
    failed = 0

    for punch in batch_req.punches:
        try:
            data = validate_and_prepare_punch(db, punch)
            log = create_punch_log(db, data)

            if arq_pool:
                try:
                    await arq_pool.enqueue_job("sync_punches_to_adms", log.id)
                except Exception:
                    pass

            results.append(BatchPunchResult(
                client_punch_id=punch.client_punch_id,
                status="success",
                log_id=log.id,
            ))
            synced += 1

        except HTTPException as e:
            results.append(BatchPunchResult(
                client_punch_id=punch.client_punch_id,
                status="error",
                error=e.detail,
            ))
            failed += 1
        except Exception as e:
            error_msg = getattr(e, 'message', str(e))
            results.append(BatchPunchResult(
                client_punch_id=punch.client_punch_id,
                status="error",
                error=error_msg,
            ))
            failed += 1

    return BatchPunchResponse(synced=synced, failed=failed, results=results)


@router.get("/api/v1/punch-types", response_model=list[PunchTypeResponse])
async def get_punch_types(
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(verify_api_key),
):
    """Mobile app fetches available punch types on startup."""
    cache_key = f"punch_types:{_.id}"
    cached = await get_cache(cache_key)
    if cached:
        return [PunchTypeResponse(**item) for item in json.loads(cached)]

    types = db.query(PunchType).filter(PunchType.is_active == True).order_by(PunchType.display_order).all()
    result = [
        PunchTypeResponse(
            code=t.code, label=t.label, adms_status_code=t.adms_status_code,
            display_order=t.display_order, icon=t.icon, color_hex=t.color_hex,
            requires_geofence=t.requires_geofence,
        ) for t in types
    ]
    await set_cache(cache_key, json.dumps([r.model_dump() for r in result]), ttl=600)
    return result


@router.get("/api/v1/punch-history")
@limiter.limit("20/minute")
async def get_punch_history(
    request: Request,
    api_key=Depends(verify_api_key),
    employee_id: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Paginated punch log history with cursor-based or offset-based pagination."""
    query = db.query(PunchLog)

    if employee_id:
        query = query.filter(PunchLog.employee_id == employee_id)

    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
            query = query.filter(PunchLog.timestamp < cursor_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor format. Use ISO timestamp.")

    logs = query.order_by(PunchLog.timestamp.desc()).offset(offset).limit(limit + 1).all()

    has_more = len(logs) > limit
    if has_more:
        logs = logs[:limit]

    next_cursor = logs[-1].timestamp.isoformat() if logs and has_more else None

    def serialize_punch_log(log):
        return {
            "id": log.id,
            "employee_id": log.employee_id,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "punch_type": log.punch_type,
            "latitude": log.latitude,
            "longitude": log.longitude,
            "is_mock_location": log.is_mock_location,
            "biometric_verified": log.biometric_verified,
            "adms_status": log.adms_status,
            "tz_offset_minutes": log.tz_offset_minutes,
        }

    return {
        "data": [serialize_punch_log(log) for log in logs],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.get("/ui/logs/export")
async def export_punch_logs(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    db: Session = Depends(get_db),
    admin=Depends(verify_api_key),  # Will be replaced with get_current_admin in main.py wiring
):
    """Export punch logs as CSV with server-side streaming for large datasets."""
    query = db.query(PunchLog, Employee.full_name).\
        outerjoin(Employee, PunchLog.employee_id == Employee.employee_id)

    if from_date:
        query = query.filter(PunchLog.timestamp >= datetime.fromisoformat(from_date))
    if to_date:
        query = query.filter(PunchLog.timestamp <= datetime.fromisoformat(to_date))

    query = query.order_by(PunchLog.timestamp).yield_per(100)

    async def generate():
        yield "Employee ID,Timestamp,Punch Type,Latitude,Longitude,Biometric,Mock Location\n"
        for log, name in query:
            yield f"{log.employee_id},{log.timestamp.isoformat() if log.timestamp else ''},{log.punch_type},{log.latitude},{log.longitude},{log.biometric_verified},{log.is_mock_location}\n"

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=attendance_logs.csv"},
    )
