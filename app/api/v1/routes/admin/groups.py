from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database.models import EmployeeGroup, AdminUser
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import EmployeeGroupCreate, EmployeeGroupUpdate, EmployeeGroupResponse
from app.api.v1.routes.admin.base import get_db, log_audit_action

router = APIRouter(tags=["Admin UI - Employee Group Management"])

@router.get("/ui/employee-groups", response_model=list[EmployeeGroupResponse])
async def get_employee_groups(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    return db.query(EmployeeGroup).order_by(EmployeeGroup.name).all()

@router.get("/ui/branches/{branch_id}/groups")
async def get_branch_groups(
    branch_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Fetch employee groups belonging to a specific branch for selectors."""
    groups = db.query(EmployeeGroup).filter(EmployeeGroup.branch_id == branch_id).order_by(EmployeeGroup.name).all()
    return [{"id": g.id, "name": g.name} for g in groups]

@router.post("/ui/employee-groups", response_model=EmployeeGroupResponse)
async def create_employee_group(
    payload: EmployeeGroupCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    group = EmployeeGroup(
        name=payload.name,
        branch_id=payload.branch_id,
        shift_schedule_id=payload.shift_schedule_id
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    
    log_audit_action(db, admin.username, "created_group", "employee_group", group.id, f"Created group {group.name} inside branch {group.branch_id}", request)
    return group

@router.put("/ui/employee-groups/{group_id}", response_model=EmployeeGroupResponse)
async def update_employee_group(
    group_id: int,
    payload: EmployeeGroupUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    group = db.query(EmployeeGroup).filter(EmployeeGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Employee group not found")
        
    if payload.name is not None:
        group.name = payload.name
    if payload.branch_id is not None:
        group.branch_id = payload.branch_id
    if payload.shift_schedule_id is not None:
        group.shift_schedule_id = payload.shift_schedule_id
        
    group.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(group)
    
    log_audit_action(db, admin.username, "updated_group", "employee_group", group.id, f"Updated group {group.name}", request)
    return group

@router.delete("/ui/employee-groups/{group_id}")
async def delete_employee_group(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    group = db.query(EmployeeGroup).filter(EmployeeGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Employee group not found")
        
    group_name = group.name
    db.delete(group)
    db.commit()
    
    log_audit_action(db, admin.username, "deleted_group", "employee_group", group_id, f"Deleted group {group_name}", request)
    return {"status": "success", "message": "Employee group deleted successfully."}
