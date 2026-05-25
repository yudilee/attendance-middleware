from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_
from datetime import date, timedelta
from typing import Optional

from app.database.models import ScheduleAssignment, ShiftSchedule, Employee, AdminUser
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import ScheduleAssignmentCreate, ScheduleAssignmentResponse
from app.api.v1.routes.admin.base import get_db, log_audit_action

router = APIRouter(tags=["Admin UI - Roster Schedule Assignments"])

@router.get("/ui/schedule-assignments", response_model=list[ScheduleAssignmentResponse])
async def get_schedule_assignments(
    employee_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    query = db.query(ScheduleAssignment)
    if employee_id:
        query = query.filter(ScheduleAssignment.employee_id == employee_id)
        
    if start_date:
        try:
            start_d = date.fromisoformat(start_date)
            query = query.filter(
                or_(
                    ScheduleAssignment.end_date >= start_d,
                    ScheduleAssignment.end_date.is_(None)
                )
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_date format. Use YYYY-MM-DD.")
            
    if end_date:
        try:
            end_d = date.fromisoformat(end_date)
            query = query.filter(ScheduleAssignment.effective_date <= end_d)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end_date format. Use YYYY-MM-DD.")

    return query.order_by(ScheduleAssignment.effective_date.desc()).all()

@router.post("/ui/schedule-assignments", response_model=ScheduleAssignmentResponse)
async def create_schedule_assignment(
    payload: ScheduleAssignmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    # Verify employee exists
    emp = db.query(Employee).filter(Employee.employee_id == payload.employee_id, Employee.is_deleted == False).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    # Verify shift exists
    shift = db.query(ShiftSchedule).filter(ShiftSchedule.id == payload.shift_schedule_id).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift schedule not found")
        
    # Overwrite exact matches or close older open-ended assignments
    db.query(ScheduleAssignment).filter(
        ScheduleAssignment.employee_id == payload.employee_id,
        ScheduleAssignment.effective_date == payload.effective_date
    ).delete()
    
    if not payload.end_date:
        existing_indefinite = db.query(ScheduleAssignment).filter(
            ScheduleAssignment.employee_id == payload.employee_id,
            ScheduleAssignment.end_date.is_(None)
        ).all()
        for old_a in existing_indefinite:
            if old_a.effective_date < payload.effective_date:
                old_a.end_date = payload.effective_date - timedelta(days=1)
            elif old_a.effective_date == payload.effective_date:
                db.delete(old_a)
                
    assignment = ScheduleAssignment(
        employee_id=payload.employee_id,
        shift_schedule_id=payload.shift_schedule_id,
        effective_date=payload.effective_date,
        end_date=payload.end_date,
        created_by=admin.username
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    
    log_audit_action(db, admin.username, "created_roster_assignment", "schedule_assignment", assignment.id, f"Assigned shift {shift.name} to employee {emp.full_name} effective {assignment.effective_date}", request)
    return assignment

@router.delete("/ui/schedule-assignments/{assignment_id}")
async def delete_schedule_assignment(
    assignment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    assignment = db.query(ScheduleAssignment).filter(ScheduleAssignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Roster assignment not found")
        
    db.delete(assignment)
    db.commit()
    
    log_audit_action(db, admin.username, "deleted_roster_assignment", "schedule_assignment", assignment_id, f"Deleted roster assignment ID {assignment_id} for employee {assignment.employee_id}", request)
    return {"status": "success", "message": "Roster assignment deleted successfully."}
