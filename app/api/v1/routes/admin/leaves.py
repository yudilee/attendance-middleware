from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.orm import Session

from app.database.models import LeaveRequest, Employee, AdminUser
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import LeaveRequestCreate, LeaveRequestUpdate, LeaveRequestResponse
from app.api.v1.routes.admin.base import get_db, log_audit_action
from app.services.webhook_service import fire_webhook

router = APIRouter(tags=["Admin UI - Leave Request Management"])

@router.get("/ui/leave-requests", response_model=list[LeaveRequestResponse])
async def get_leave_requests(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    return db.query(LeaveRequest).order_by(LeaveRequest.created_at.desc()).all()

@router.post("/ui/leave-requests", response_model=LeaveRequestResponse)
async def create_leave_request(
    payload: LeaveRequestCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    emp = db.query(Employee).filter(Employee.employee_id == payload.employee_id, Employee.is_deleted == False).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    leave = LeaveRequest(
        employee_id=payload.employee_id,
        leave_type=payload.leave_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        reason=payload.reason,
        status="approved", # Admin created is auto-approved
        approved_by=admin.username
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)
    
    arq_pool = getattr(request.app.state, "arq_pool", None)
    background_tasks.add_task(
        fire_webhook,
        db,
        "leave.approved",
        {
            "id": leave.id,
            "employee_id": leave.employee_id,
            "leave_type": leave.leave_type,
            "start_date": leave.start_date.isoformat() if leave.start_date else None,
            "end_date": leave.end_date.isoformat() if leave.end_date else None,
            "reason": leave.reason,
            "status": leave.status,
            "approved_by": leave.approved_by
        },
        arq_pool
    )
    
    log_audit_action(db, admin.username, "created_leave", "leave_request", leave.id, f"Created approved leave request for {leave.employee_id} ({leave.start_date} to {leave.end_date})", request)
    return leave

@router.put("/ui/leave-requests/{leave_id}", response_model=LeaveRequestResponse)
async def review_leave_request(
    leave_id: int,
    payload: LeaveRequestUpdate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    leave = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")
        
    leave.status = payload.status
    leave.approved_by = admin.username
    db.commit()
    db.refresh(leave)
    
    if leave.status == "approved":
        arq_pool = getattr(request.app.state, "arq_pool", None)
        background_tasks.add_task(
            fire_webhook,
            db,
            "leave.approved",
            {
                "id": leave.id,
                "employee_id": leave.employee_id,
                "leave_type": leave.leave_type,
                "start_date": leave.start_date.isoformat() if leave.start_date else None,
                "end_date": leave.end_date.isoformat() if leave.end_date else None,
                "reason": leave.reason,
                "status": leave.status,
                "approved_by": leave.approved_by
            },
            arq_pool
        )
    
    log_audit_action(db, admin.username, f"reviewed_leave_{payload.status}", "leave_request", leave.id, f"Reviewed leave request for {leave.employee_id} status={payload.status}", request)
    return leave

@router.delete("/ui/leave-requests/{leave_id}")
async def delete_leave_request(
    leave_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    leave = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")
        
    db.delete(leave)
    db.commit()
    
    log_audit_action(db, admin.username, "deleted_leave", "leave_request", leave_id, "Deleted leave request record", request)
    return {"status": "success", "message": "Leave request deleted successfully."}
