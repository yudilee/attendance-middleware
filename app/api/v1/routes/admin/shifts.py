from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database.models import ShiftSchedule, AdminUser
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import ShiftScheduleCreate, ShiftScheduleUpdate, ShiftScheduleResponse
from app.api.v1.routes.admin.base import get_db, log_audit_action

router = APIRouter(tags=["Admin UI - Shift Schedule Management"])

@router.get("/ui/shift-schedules", response_model=list[ShiftScheduleResponse])
async def get_shift_schedules(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    return db.query(ShiftSchedule).order_by(ShiftSchedule.name).all()

@router.post("/ui/shift-schedules", response_model=ShiftScheduleResponse)
async def create_shift_schedule(
    payload: ShiftScheduleCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    if payload.is_default:
        db.query(ShiftSchedule).update({"is_default": False})
        
    schedule = ShiftSchedule(
        name=payload.name,
        start_time=payload.start_time,
        end_time=payload.end_time,
        grace_minutes=payload.grace_minutes,
        min_work_hours=payload.min_work_hours,
        overtime_after_hours=payload.overtime_after_hours,
        working_days=payload.working_days,
        is_default=payload.is_default,
        schedule_type=payload.schedule_type,
        interval_days=payload.interval_days,
        anchor_date=payload.anchor_date,
        overtime_multiplier_1=payload.overtime_multiplier_1,
        overtime_multiplier_2=payload.overtime_multiplier_2,
        overtime_threshold_2_hours=payload.overtime_threshold_2_hours,
        weekend_overtime_multiplier=payload.weekend_overtime_multiplier,
        holiday_overtime_multiplier=payload.holiday_overtime_multiplier,
        monthly_overtime_cap_hours=payload.monthly_overtime_cap_hours,
        auto_clockout_enabled=payload.auto_clockout_enabled,
        auto_clockout_buffer_minutes=payload.auto_clockout_buffer_minutes
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    
    log_audit_action(db, admin.username, "created_shift", "shift_schedule", schedule.id, f"Created shift schedule {schedule.name} ({schedule.start_time}-{schedule.end_time})", request)
    return schedule

@router.put("/ui/shift-schedules/{schedule_id}", response_model=ShiftScheduleResponse)
async def update_shift_schedule(
    schedule_id: int,
    payload: ShiftScheduleUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    schedule = db.query(ShiftSchedule).filter(ShiftSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Shift schedule not found")
        
    if payload.is_default is True:
        db.query(ShiftSchedule).filter(ShiftSchedule.id != schedule_id).update({"is_default": False})
        schedule.is_default = True
    elif payload.is_default is False:
        schedule.is_default = False
        
    if payload.name is not None:
        schedule.name = payload.name
    if payload.start_time is not None:
        schedule.start_time = payload.start_time
    if payload.end_time is not None:
        schedule.end_time = payload.end_time
    if payload.grace_minutes is not None:
        schedule.grace_minutes = payload.grace_minutes
    if payload.min_work_hours is not None:
        schedule.min_work_hours = payload.min_work_hours
    if payload.overtime_after_hours is not None:
        schedule.overtime_after_hours = payload.overtime_after_hours
    if payload.working_days is not None:
        schedule.working_days = payload.working_days
    if payload.schedule_type is not None:
        schedule.schedule_type = payload.schedule_type
    if payload.interval_days is not None:
        schedule.interval_days = payload.interval_days
    if payload.anchor_date is not None:
        schedule.anchor_date = payload.anchor_date
    if payload.overtime_multiplier_1 is not None:
        schedule.overtime_multiplier_1 = payload.overtime_multiplier_1
    if payload.overtime_multiplier_2 is not None:
        schedule.overtime_multiplier_2 = payload.overtime_multiplier_2
    if payload.overtime_threshold_2_hours is not None:
        schedule.overtime_threshold_2_hours = payload.overtime_threshold_2_hours
    if payload.weekend_overtime_multiplier is not None:
        schedule.weekend_overtime_multiplier = payload.weekend_overtime_multiplier
    if payload.holiday_overtime_multiplier is not None:
        schedule.holiday_overtime_multiplier = payload.holiday_overtime_multiplier
    if payload.monthly_overtime_cap_hours is not None:
        schedule.monthly_overtime_cap_hours = payload.monthly_overtime_cap_hours
    if payload.auto_clockout_enabled is not None:
        schedule.auto_clockout_enabled = payload.auto_clockout_enabled
    if payload.auto_clockout_buffer_minutes is not None:
        schedule.auto_clockout_buffer_minutes = payload.auto_clockout_buffer_minutes
        
    db.commit()
    from app.services.cached_lookups import invalidate_shift_cache
    invalidate_shift_cache(schedule_id)
    db.refresh(schedule)
    
    log_audit_action(db, admin.username, "updated_shift", "shift_schedule", schedule.id, f"Updated shift schedule {schedule.name}", request)
    return schedule

@router.delete("/ui/shift-schedules/{schedule_id}")
async def delete_shift_schedule(
    schedule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    schedule = db.query(ShiftSchedule).filter(ShiftSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail="Shift schedule not found")
        
    schedule_name = schedule.name
    db.delete(schedule)
    db.commit()
    from app.services.cached_lookups import invalidate_shift_cache
    invalidate_shift_cache(schedule_id)
    
    log_audit_action(db, admin.username, "deleted_shift", "shift_schedule", schedule_id, f"Deleted shift schedule {schedule_name}", request)
    return {"status": "success", "message": "Shift schedule deleted successfully."}
