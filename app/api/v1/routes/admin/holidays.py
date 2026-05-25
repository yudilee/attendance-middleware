from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database.models import Holiday, AdminUser
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import HolidayCreate, HolidayResponse
from app.api.v1.routes.admin.base import get_db, log_audit_action

router = APIRouter(tags=["Admin UI - Holiday Management"])

@router.get("/ui/holidays", response_model=list[HolidayResponse])
async def get_holidays(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    return db.query(Holiday).order_by(Holiday.date.desc()).all()

@router.post("/ui/holidays", response_model=HolidayResponse)
async def create_holiday(
    payload: HolidayCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    existing = db.query(Holiday).filter(Holiday.date == payload.date).first()
    if existing:
        raise HTTPException(status_code=400, detail="Holiday for this date already exists.")
        
    holiday = Holiday(
        name=payload.name,
        date=payload.date,
        is_recurring=payload.is_recurring
    )
    db.add(holiday)
    db.commit()
    from app.services.cached_lookups import invalidate_holiday_cache
    invalidate_holiday_cache(holiday.date.year)
    db.refresh(holiday)
    
    log_audit_action(db, admin.username, "created_holiday", "holiday", holiday.id, f"Created holiday {holiday.name} on {holiday.date}", request)
    return holiday

@router.delete("/ui/holidays/{holiday_id}")
async def delete_holiday(
    holiday_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    holiday = db.query(Holiday).filter(Holiday.id == holiday_id).first()
    if not holiday:
        raise HTTPException(status_code=404, detail="Holiday not found")
        
    holiday_name = holiday.name
    db.delete(holiday)
    db.commit()
    from app.services.cached_lookups import invalidate_holiday_cache
    invalidate_holiday_cache(holiday.date.year)
    
    log_audit_action(db, admin.username, "deleted_holiday", "holiday", holiday_id, f"Deleted holiday {holiday_name}", request)
    return {"status": "success", "message": "Holiday deleted successfully."}
