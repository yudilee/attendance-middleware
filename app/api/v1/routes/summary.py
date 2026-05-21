"""
Attendance summary API endpoint.

Returns pre-aggregated daily, weekly, and monthly attendance stats for
a given employee, computed from the authoritative server-side PunchLog data.

GET /api/v1/attendance/summary?employee_id=&period=today|week|month
"""
import structlog
from datetime import datetime, timedelta
from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.models import SessionLocal, PunchLog, DeviceBinding
from app.services.auth import verify_api_key

logger = structlog.get_logger()
router = APIRouter(tags=["Attendance"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _compute_summary(punches: list) -> dict:
    """Compute work hours and late-arrival status from an ordered punch list."""
    if not punches:
        return {"total_hours": 0.0, "first_in": None, "last_out": None, "is_late": False}

    clock_ins = [p for p in punches if p.punch_type and "in" in p.punch_type.lower()]
    clock_outs = [p for p in punches if p.punch_type and "out" in p.punch_type.lower()]

    first_in = clock_ins[0].timestamp if clock_ins else punches[0].timestamp
    last_out = clock_outs[-1].timestamp if clock_outs else punches[-1].timestamp

    total_hours = (last_out - first_in).total_seconds() / 3600 if last_out > first_in else 0.0
    is_late = first_in.hour >= 9  # configurable: assumes 09:00 cutoff

    return {
        "total_hours": round(total_hours, 2),
        "first_in": first_in.isoformat(),
        "last_out": last_out.isoformat(),
        "is_late": is_late,
    }


@router.get("/api/v1/attendance/summary")
async def get_attendance_summary(
    employee_id: Optional[str] = None,
    period: str = "today",
    device_uuid: Optional[str] = None,
    db: Session = Depends(get_db),
    api_key=Depends(verify_api_key),
):
    """
    Return pre-aggregated attendance statistics for an employee.

    - **period**: `today` | `week` | `month`
    - **employee_id**: resolve from device_uuid if omitted
    """
    # Resolve employee_id from device if not supplied
    if not employee_id and device_uuid:
        binding = db.query(DeviceBinding).filter(
            DeviceBinding.device_uuid == device_uuid
        ).first()
        if binding:
            employee_id = binding.employee_id

    if not employee_id:
        raise HTTPException(status_code=400, detail="employee_id or device_uuid required")

    now = datetime.utcnow()

    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif period == "week":
        # ISO week: Monday to Sunday
        days_since_monday = now.weekday()
        start = (now - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # First day of next month
        if now.month == 12:
            end = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            end = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise HTTPException(status_code=400, detail="period must be one of: today, week, month")

    punches = db.query(PunchLog).filter(
        PunchLog.employee_id == employee_id,
        PunchLog.timestamp >= start,
        PunchLog.timestamp < end,
    ).order_by(PunchLog.timestamp).all()

    # Group punches by date for daily breakdown
    daily: dict[str, list] = {}
    for p in punches:
        day_key = p.timestamp.date().isoformat()
        daily.setdefault(day_key, []).append(p)

    daily_breakdown = []
    total_present = 0
    total_late = 0
    total_work_hours = 0.0

    for day_str, day_punches in sorted(daily.items()):
        summary = _compute_summary(day_punches)
        punched = len(day_punches) > 0
        if punched:
            total_present += 1
        if summary["is_late"]:
            total_late += 1
        total_work_hours += summary["total_hours"]

        daily_breakdown.append({
            "date": day_str,
            "status": "present" if punched else "absent",
            "hours": summary["total_hours"],
            "late": summary["is_late"],
            "first_in": summary["first_in"],
            "last_out": summary["last_out"],
            "punch_count": len(day_punches),
        })

    # Today's specific info (for the dashboard "shift status" card)
    today_punches = daily.get(now.date().isoformat(), [])
    today_summary = _compute_summary(today_punches)

    return {
        "employee_id": employee_id,
        "period": period,
        "start": start.date().isoformat(),
        "end": (end - timedelta(days=1)).date().isoformat(),
        "total_present": total_present,
        "total_late": total_late,
        "total_work_hours": round(total_work_hours, 2),
        "today": {
            "clock_in_time": today_summary["first_in"],
            "clock_out_time": today_summary["last_out"],
            "hours_today": today_summary["total_hours"],
            "is_late": today_summary["is_late"],
            "punch_count": len(today_punches),
        },
        "daily_breakdown": daily_breakdown,
    }
