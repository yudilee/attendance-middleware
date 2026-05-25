"""
Employee Self-Service Portal Routes.
Enables employees to log in, view historical punches, request leave, and submit punch corrections.
"""
from datetime import datetime, date, timedelta
from typing import Optional
import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database.models import (
    SessionLocal, Employee, PunchLog, LeaveRequest, AttendanceCorrection, 
    DeviceBinding, Branch, Company, LeaveBalance, OvertimeRequest
)
from app.services.report_service import pair_employee_punches

router = APIRouter(tags=["Employee Portal"])

# Setup templates path
template_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "templates")
templates = Jinja2Templates(directory=template_path)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_employee(request: Request, db: Session = Depends(get_db)) -> Employee:
    """Helper to authenticate employee via cookie session."""
    emp_id = request.cookies.get("portal_session")
    if not emp_id:
        raise HTTPException(status_code=302, headers={"Location": "/portal"})
    
    emp = db.query(Employee).filter(Employee.employee_id == emp_id, Employee.is_deleted == False, Employee.is_active == True).first()
    if not emp:
        raise HTTPException(status_code=302, headers={"Location": "/portal"})
    return emp


# ─── Auth Routes ─────────────────────────────────────────────────────────────

@router.get("/portal", response_class=HTMLResponse)
async def portal_login_page(request: Request, error: Optional[str] = None):
    # If already logged in, redirect to dashboard
    emp_id = request.cookies.get("portal_session")
    if emp_id:
        return RedirectResponse(url="/portal/dashboard", status_code=302)
    return templates.TemplateResponse(request=request, name="portal_login.html", context={"error": error})


@router.post("/portal/login")
async def portal_login_submit(
    response: Response,
    employee_id: str = Form(...),
    db: Session = Depends(get_db)
):
    emp = db.query(Employee).filter(Employee.employee_id == employee_id, Employee.is_deleted == False).first()
    if not emp:
        return RedirectResponse(url="/portal?error=Invalid Employee ID or PIN", status_code=302)
    
    if not emp.is_active:
        return RedirectResponse(url="/portal?error=Employee account is suspended", status_code=302)

    # Set simple cookie session (valid for 1 day)
    response = RedirectResponse(url="/portal/dashboard", status_code=302)
    response.set_cookie(key="portal_session", value=emp.employee_id, max_age=86400, httponly=True)
    return response


@router.get("/portal/logout")
async def portal_logout(response: Response):
    response = RedirectResponse(url="/portal", status_code=302)
    response.delete_cookie(key="portal_session")
    return response


# ─── Dashboard & self-service ──────────────────────────────────────────────────

@router.get("/portal/dashboard", response_class=HTMLResponse)
async def portal_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee)
):
    today = date.today()
    # Pull punches for the last 30 days
    start_d = today - timedelta(days=30)
    end_d = today
    
    paired_data = pair_employee_punches(db, employee, start_d, end_d)
    
    # Extract today's records
    today_rec = None
    for rec in paired_data.get("daily_records", []):
        if rec["date"] == today:
            today_rec = rec
            break
            
    # Calculate simple stats from 30 day history
    total_present = paired_data.get("total_present", 0)
    total_late = paired_data.get("total_late", 0)
    total_hours = paired_data.get("total_hours", 0.0)
    
    # Pull correction filings
    corrections = db.query(AttendanceCorrection).filter(
        AttendanceCorrection.employee_id == employee.employee_id
    ).order_by(AttendanceCorrection.created_at.desc()).all()
    
    # Pull leave requests
    leaves = db.query(LeaveRequest).filter(
        LeaveRequest.employee_id == employee.employee_id
    ).order_by(LeaveRequest.created_at.desc()).all()

    # Find the employee's assigned branches to show geofence list
    branches = []
    binding = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee.employee_id, DeviceBinding.is_active == True).first()
    if binding:
        from app.database.models import BindingBranch
        assigned = db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).all()
        for ba in assigned:
            br = db.query(Branch).filter(Branch.id == ba.branch_id).first()
            if br:
                branches.append(br)
    
    return templates.TemplateResponse(
        request=request,
        name="portal_dashboard.html",
        context={
            "employee": employee,
            "today_rec": today_rec,
            "total_present": total_present,
            "total_late": total_late,
            "total_hours": total_hours,
            "daily_records": paired_data.get("daily_records", [])[::-1], # most recent first
            "corrections": corrections,
            "leaves": leaves,
            "branches": branches
        }
    )


@router.post("/portal/leave-request")
async def portal_submit_leave(
    leave_type: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee)
):
    try:
        start_d = date.fromisoformat(start_date)
        end_d = date.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    leave = LeaveRequest(
        employee_id=employee.employee_id,
        leave_type=leave_type,
        start_date=start_d,
        end_date=end_d,
        reason=reason,
        status="pending"
    )
    db.add(leave)
    db.commit()
    return RedirectResponse(url="/portal/dashboard?success=Leave request submitted successfully", status_code=302)


@router.post("/portal/correction")
async def portal_submit_correction(
    correction_type: str = Form(...),
    proposed_date: str = Form(...),
    proposed_time: str = Form(...),
    proposed_punch_type: str = Form(...),
    description: str = Form(...),
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee)
):
    try:
        dt_str = f"{proposed_date} {proposed_time}"
        proposed_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date/time format")

    correction = AttendanceCorrection(
        employee_id=employee.employee_id,
        correction_type=correction_type,
        proposed_timestamp=proposed_dt,
        proposed_punch_type=proposed_punch_type,
        description=description,
        status="pending"
    )
    db.add(correction)
    db.commit()
    return RedirectResponse(url="/portal/dashboard?success=Punch correction submitted successfully", status_code=302)


# ─── Employee Portal Phase 6 Enhancements ───────────────────────────────────

@router.get("/portal/leave-balance")
async def get_leave_balance(
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee)
):
    """Retrieve or create leave balance for current employee and current year."""
    current_year = datetime.now().year
    balance = db.query(LeaveBalance).filter(
        LeaveBalance.employee_id == employee.employee_id,
        LeaveBalance.year == current_year
    ).first()
    
    if not balance:
        # Create default balance
        balance = LeaveBalance(
            employee_id=employee.employee_id,
            year=current_year,
            annual_total=12,
            annual_used=0,
            sick_total=12,
            sick_used=0
        )
        db.add(balance)
        db.commit()
        db.refresh(balance)
        
    return {
        "year": balance.year,
        "annual_total": balance.annual_total,
        "annual_used": balance.annual_used,
        "annual_remaining": balance.annual_total - balance.annual_used,
        "sick_total": balance.sick_total,
        "sick_used": balance.sick_used,
        "sick_remaining": balance.sick_total - balance.sick_used
    }


@router.get("/portal/attendance-export")
async def get_attendance_export(
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee)
):
    """Export employee's attendance history for the last 30 days as a CSV file."""
    import csv
    import io
    from fastapi.responses import StreamingResponse
    
    today = date.today()
    start_d = today - timedelta(days=30)
    end_d = today
    
    paired_data = pair_employee_punches(db, employee, start_d, end_d)
    records = paired_data.get("daily_records", [])
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow([
        "Date", "Roster/Shift", "First In", "Last Out", 
        "Work Duration (hrs)", "Break Duration (hrs)", 
        "Overtime (hrs)", "Status", "Notes"
    ])
    
    for rec in records:
        writer.writerow([
            rec["date"].isoformat() if isinstance(rec["date"], date) else str(rec["date"]),
            rec.get("shift_name", "Off Day"),
            rec.get("first_in").strftime("%H:%M:%S") if rec.get("first_in") else "-",
            rec.get("last_out").strftime("%H:%M:%S") if rec.get("last_out") else "-",
            round(rec.get("work_duration_hours", 0.0), 2),
            round(rec.get("break_duration_hours", 0.0), 2),
            round(rec.get("overtime_hours", 0.0), 2),
            rec.get("status", "Present"),
            rec.get("notes", "")
        ])
    
    output.seek(0)
    
    filename = f"attendance_history_{employee.employee_id}_{today.isoformat()}.csv"
    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"'
    }
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers=headers)


@router.post("/portal/overtime-request")
async def submit_overtime_request(
    overtime_date: str = Form(...),
    hours_requested: float = Form(...),
    reason: str = Form(""),
    db: Session = Depends(get_db),
    employee: Employee = Depends(get_current_employee)
):
    """File a pre-approved overtime request to supervisor."""
    try:
        ot_date = date.fromisoformat(overtime_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Expected YYYY-MM-DD.")
        
    if hours_requested <= 0:
        raise HTTPException(status_code=400, detail="Hours requested must be greater than zero.")
        
    ot_request = OvertimeRequest(
        employee_id=employee.employee_id,
        date=ot_date,
        hours_requested=hours_requested,
        reason=reason,
        status="pending"
    )
    db.add(ot_request)
    db.commit()
    return RedirectResponse(
        url="/portal/dashboard?success=Overtime request submitted successfully", 
        status_code=302
    )
