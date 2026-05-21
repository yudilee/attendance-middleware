"""Supervisor/manager API routes: team attendance, corrections, review."""
import structlog
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.models import (
    SessionLocal, DeviceBinding, EmployeeSupervisor, Employee,
    PunchLog, AttendanceCorrection,
)
from app.services.auth import verify_api_key
from app.api.v1.schemas import (
    TeamAttendanceResponse, CorrectionRequest, CorrectionReview,
)
from app.services.notification_service import send_correction_result

logger = structlog.get_logger()

router = APIRouter(tags=["Supervisor"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def resolve_binding(api_key, device_uuid: Optional[str], db: Session):
    """Find device binding from API key or device UUID."""
    binding = db.query(DeviceBinding).filter(
        DeviceBinding.api_key_id == api_key.id
    ).first()
    if device_uuid:
        binding = db.query(DeviceBinding).filter(
            DeviceBinding.device_uuid == device_uuid
        ).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    return binding


@router.get("/api/v1/supervisor/team")
async def get_team_attendance(
    request: "Request",
    device_uuid: Optional[str] = None,
    date: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    api_key=Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Get attendance status of all team members for a supervisor with pagination."""
    binding = resolve_binding(api_key, device_uuid, db)
    supervisor_id = binding.employee_id

    team = db.query(EmployeeSupervisor).filter(
        EmployeeSupervisor.supervisor_id == supervisor_id
    ).offset(offset).limit(limit).all()

    if not team:
        return {"team": []}

    employee_ids = [t.employee_id for t in team]
    employees = db.query(Employee).filter(Employee.employee_id.in_(employee_ids)).all()
    employee_map = {e.employee_id: e.full_name for e in employees}

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    if date:
        today = datetime.fromisoformat(date).replace(hour=0, minute=0, second=0, microsecond=0)

    tomorrow = today + timedelta(days=1)

    result = []
    for emp_id in employee_ids:
        punches = db.query(PunchLog).filter(
            PunchLog.employee_id == emp_id,
            PunchLog.timestamp >= today,
            PunchLog.timestamp < tomorrow,
        ).order_by(PunchLog.timestamp).all()

        first_punch = punches[0] if punches else None
        last_punch = punches[-1] if punches else None

        total_hours = None
        if len(punches) >= 2:
            total_seconds = (last_punch.timestamp - first_punch.timestamp).total_seconds()
            total_hours = round(total_seconds / 3600, 2)

        result.append({
            "employee_id": emp_id,
            "name": employee_map.get(emp_id, emp_id),
            "today_punched": len(punches) > 0,
            "first_punch_time": first_punch.timestamp.isoformat() if first_punch else None,
            "last_punch_time": last_punch.timestamp.isoformat() if last_punch else None,
            "total_hours_today": total_hours,
            "is_late": first_punch and first_punch.timestamp.hour >= 9,
        })

    return {"team": result, "date": today.date().isoformat()}


@router.get("/api/v1/supervisor/team/{employee_id}/history")
async def get_employee_history(
    employee_id: str,
    days: int = 7,
    limit: int = 50,
    offset: int = 0,
    device_uuid: Optional[str] = None,
    api_key=Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Get detailed punch history for a specific team member with pagination."""
    start_date = datetime.utcnow() - timedelta(days=days)

    punches = db.query(PunchLog).filter(
        PunchLog.employee_id == employee_id,
        PunchLog.timestamp >= start_date,
    ).order_by(PunchLog.timestamp.desc()).offset(offset).limit(limit).all()

    return {
        "employee_id": employee_id,
        "days": days,
        "punches": [
            {
                "id": p.id,
                "timestamp": p.timestamp.isoformat(),
                "punch_type": p.punch_type,
                "latitude": p.latitude,
                "longitude": p.longitude,
                "biometric_verified": p.biometric_verified,
                "is_mock_location": p.is_mock_location,
            }
            for p in punches
        ],
    }


@router.post("/api/v1/attendance/correction")
async def request_correction(
    request_data: CorrectionRequest,
    api_key=Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Submit an attendance correction request."""
    correction = AttendanceCorrection(
        employee_id=request_data.employee_id,
        original_punch_id=request_data.original_punch_id,
        correction_type=request_data.correction_type,
        description=request_data.description,
        proposed_timestamp=datetime.fromisoformat(request_data.proposed_timestamp) if request_data.proposed_timestamp else None,
        proposed_punch_type=request_data.proposed_punch_type,
        status='pending',
    )
    db.add(correction)
    db.commit()
    db.refresh(correction)
    return {"status": "submitted", "correction_id": correction.id}


@router.get("/api/v1/supervisor/corrections")
async def get_pending_corrections(
    limit: int = 50,
    offset: int = 0,
    device_uuid: Optional[str] = None,
    api_key=Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Get pending correction requests for the supervisor's team with pagination."""
    binding = resolve_binding(api_key, device_uuid, db)

    team = db.query(EmployeeSupervisor).filter(
        EmployeeSupervisor.supervisor_id == binding.employee_id
    ).all()
    employee_ids = [t.employee_id for t in team]

    corrections = db.query(AttendanceCorrection).filter(
        AttendanceCorrection.employee_id.in_(employee_ids),
        AttendanceCorrection.status == 'pending',
    ).order_by(AttendanceCorrection.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "corrections": [
            {
                "id": c.id,
                "employee_id": c.employee_id,
                "correction_type": c.correction_type,
                "description": c.description,
                "proposed_timestamp": c.proposed_timestamp.isoformat() if c.proposed_timestamp else None,
                "proposed_punch_type": c.proposed_punch_type,
                "created_at": c.created_at.isoformat(),
            }
            for c in corrections
        ]
    }


@router.post("/api/v1/supervisor/corrections/{correction_id}/review")
async def review_correction(
    correction_id: int,
    review: CorrectionReview,
    device_uuid: Optional[str] = None,
    api_key=Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Approve or reject an attendance correction request."""
    correction = db.query(AttendanceCorrection).filter(
        AttendanceCorrection.id == correction_id
    ).first()

    if not correction:
        raise HTTPException(status_code=404, detail="Correction not found")

    binding = resolve_binding(api_key, device_uuid, db)

    correction.status = review.status
    correction.reviewed_by = binding.employee_id if binding else "unknown"
    correction.reviewed_at = datetime.utcnow()
    correction.review_notes = review.notes

    if review.status == 'approved' and correction.original_punch_id:
        punch = db.query(PunchLog).filter(PunchLog.id == correction.original_punch_id).first()
        if punch:
            if correction.proposed_punch_type:
                punch.punch_type = correction.proposed_punch_type
            if correction.proposed_timestamp:
                punch.timestamp = correction.proposed_timestamp

    db.commit()

    try:
        # Notify employee
        employee_bindings = db.query(DeviceBinding).filter(
            DeviceBinding.employee_id == correction.employee_id,
            DeviceBinding.fcm_token.isnot(None)
        ).all()
        for emp_binding in employee_bindings:
            send_correction_result(
                fcm_token=emp_binding.fcm_token,
                is_approved=(review.status == 'approved'),
                log_id=correction.original_punch_id
            )
    except Exception as e:
        logger.error(f"Failed to send correction notification: {e}")

    return {"status": review.status, "correction_id": correction_id}
