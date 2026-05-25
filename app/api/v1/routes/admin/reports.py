import io
import os
from datetime import datetime, timedelta, date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database.models import PunchLog, Employee, Branch, DeviceBinding, BindingBranch, Holiday, LeaveRequest, AdminUser, ShiftSchedule, EmployeeGroup, Company
from app.services.auth_ui import get_current_admin
from app.services.report_service import pair_employee_punches, generate_excel_report
from app.services.geo import is_within_any_fence
from app.api.v1.routes.admin.base import get_db, templates, log_audit_action

router = APIRouter(tags=["Admin UI - Reports & Export"])

# Selfie Serving
@router.get("/ui/selfie/{filename}")
async def get_selfie(
    filename: str,
    current_user: AdminUser = Depends(get_current_admin),
):
    # app/api/v1/routes/admin -> app -> backend -> uploads/selfies
    current_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))))
    upload_dir = os.path.join(backend_dir, "uploads", "selfies")
    filepath = os.path.join(upload_dir, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Selfie not found")
    return FileResponse(filepath, media_type="image/jpeg")

# Detailed work hours report
@router.get("/ui/reports/work-hours")
async def get_work_hours_report(
    from_date: str,
    to_date: str,
    company_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    group_id: Optional[int] = None,
    department: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    try:
        start_d = date.fromisoformat(from_date)
        end_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    # Fetch employees applying filters
    query = db.query(Employee).filter(Employee.is_deleted == False)
    if company_id:
        query = query.filter(Employee.company_id == company_id)
    if group_id:
        query = query.filter(Employee.group_id == group_id)
    if department:
        query = query.filter(Employee.department == department)
        
    if branch_id:
        bindings = db.query(DeviceBinding.employee_id).outerjoin(
            BindingBranch, DeviceBinding.id == BindingBranch.binding_id
        ).filter(
            or_(DeviceBinding.branch_id == branch_id, BindingBranch.branch_id == branch_id)
        ).subquery()
        query = query.filter(Employee.employee_id.in_(bindings))

    employees = query.order_by(Employee.full_name).all()
    
    # Preloaded optimization to eliminate N+1 queries
    all_shifts = {s.id: s for s in db.query(ShiftSchedule).all()}
    all_groups = {g.id: g for g in db.query(EmployeeGroup).all()}
    all_branches = {b.id: b for b in db.query(Branch).all()}
    all_companies = {c.id: c for c in db.query(Company).all()}
    default_shift = db.query(ShiftSchedule).filter(ShiftSchedule.is_default == True).first()
    
    bindings_raw = db.query(DeviceBinding).filter(DeviceBinding.is_active == True).all()
    preloaded_bindings = {b.employee_id: b for b in bindings_raw if b.employee_id}
    
    binding_branches_raw = db.query(BindingBranch).all()
    preloaded_binding_branches = {bb.binding_id: bb for bb in binding_branches_raw}
    
    preloaded_holidays = db.query(Holiday).filter(Holiday.date >= start_d, Holiday.date <= end_d).all()
    
    leaves_raw = db.query(LeaveRequest).filter(
        LeaveRequest.status == "approved",
        LeaveRequest.start_date <= end_d,
        LeaveRequest.end_date >= start_d
    ).all()
    preloaded_leaves_by_emp = {}
    for l in leaves_raw:
        preloaded_leaves_by_emp.setdefault(l.employee_id, []).append(l)
        
    emp_ids = [emp.employee_id for emp in employees]
    
    # Preload ScheduleAssignments (Phase 5D)
    preloaded_assignments_by_emp = {}
    if emp_ids:
        from app.database.models import ScheduleAssignment
        assignments_raw = db.query(ScheduleAssignment).filter(
            ScheduleAssignment.employee_id.in_(emp_ids),
            ScheduleAssignment.effective_date <= end_d,
            or_(ScheduleAssignment.end_date >= start_d, ScheduleAssignment.end_date.is_(None))
        ).order_by(ScheduleAssignment.effective_date.desc()).all()
        for a in assignments_raw:
            preloaded_assignments_by_emp.setdefault(a.employee_id, []).append(a)

    db_start = datetime.combine(start_d - timedelta(days=1), datetime.min.time())
    db_end = datetime.combine(end_d + timedelta(days=1), datetime.max.time())
    
    if emp_ids:
        punches_raw = db.query(PunchLog).filter(
            PunchLog.employee_id.in_(emp_ids),
            PunchLog.timestamp >= db_start,
            PunchLog.timestamp <= db_end
        ).order_by(PunchLog.timestamp).all()
    else:
        punches_raw = []
        
    preloaded_punches_by_emp = {}
    for p in punches_raw:
        preloaded_punches_by_emp.setdefault(p.employee_id, []).append(p)
        
    results = []
    for emp in employees:
        emp_punches = preloaded_punches_by_emp.get(emp.employee_id, [])
        emp_leaves = preloaded_leaves_by_emp.get(emp.employee_id, [])
        emp_assignments = preloaded_assignments_by_emp.get(emp.employee_id, [])
        paired = pair_employee_punches(
            db, emp, start_d, end_d,
            preloaded_shifts=all_shifts,
            preloaded_groups=all_groups,
            preloaded_branches=all_branches,
            preloaded_companies=all_companies,
            preloaded_bindings=preloaded_bindings,
            preloaded_binding_branches=preloaded_binding_branches,
            preloaded_default_shift=default_shift,
            preloaded_holidays=preloaded_holidays,
            preloaded_leaves=emp_leaves,
            preloaded_punches=emp_punches,
            preloaded_assignments=emp_assignments,
        )
        results.append(paired)
        
    return results

# Combined Excel / CSV / Print PDF Export
@router.get("/ui/reports/export")
async def export_reports(
    request: Request,
    format: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    branch_id: Optional[int] = None,
    company_id: Optional[int] = None,
    group_id: Optional[int] = None,
    department: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    actual_start = start_date or from_date
    actual_end = end_date or to_date

    # If no dates are provided at all, fallback to last 30 days
    if not actual_start or not actual_end:
        today = date.today()
        actual_start = actual_start or (today - timedelta(days=30)).isoformat()
        actual_end = actual_end or today.isoformat()

    # Excel format check: if format is xlsx or if from_date/to_date are provided and format is not csv/print
    if format == "xlsx" or (from_date and to_date and format not in ["csv", "print"]):
        try:
            start_d = date.fromisoformat(actual_start)
            end_d = date.fromisoformat(actual_end)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

        wb = generate_excel_report(
            db, start_d, end_d,
            company_id=company_id,
            branch_id=branch_id,
            group_id=group_id,
            department=department
        )
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        
        log_audit_action(db, admin.username, "exported_excel_report", "report", None, f"Exported attendance report from {actual_start} to {actual_end}")
        filename = f"attendance_report_{actual_start}_to_{actual_end}.xlsx"
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    
    # CSV / HTML print logic
    def parse_date(date_str: str) -> Optional[datetime]:
        if not date_str:
            return None
        try:
            if len(date_str) <= 10:
                return datetime.strptime(date_str, "%Y-%m-%d")
            return datetime.fromisoformat(date_str.replace("Z", ""))
        except Exception:
            return None

    query = db.query(PunchLog, Employee.full_name).\
        outerjoin(Employee, PunchLog.employee_id == Employee.employee_id)

    start_dt = parse_date(actual_start)
    if start_dt:
        query = query.filter(PunchLog.timestamp >= start_dt)

    end_dt = parse_date(actual_end)
    if end_dt:
        if len(actual_end) <= 10:
            end_dt = end_dt + timedelta(days=1) - timedelta(seconds=1)
        query = query.filter(PunchLog.timestamp <= end_dt)

    query = query.order_by(PunchLog.timestamp.desc())
    logs_raw = query.all()
    branches = db.query(Branch).all()

    target_branch = None
    if branch_id:
        target_branch = db.query(Branch).filter(Branch.id == branch_id).first()

    processed_logs = []
    in_count = 0
    out_count = 0

    for log, name in logs_raw:
        in_fence, dist, r_branch_name = is_within_any_fence(log.latitude, log.longitude, branches, db=db)
        if not in_fence:
            r_branch_name = "Off-site"

        if branch_id and target_branch:
            in_target_fence, _, _ = is_within_any_fence(log.latitude, log.longitude, [target_branch], db=db)
            if not in_target_fence:
                continue

        log.employee_name = name or "Unknown"
        log.resolved_branch_name = r_branch_name
        processed_logs.append(log)

        if log.punch_type.lower() == "in":
            in_count += 1
        else:
            out_count += 1

    if format == "print":
        branch_name_label = target_branch.name if target_branch else "All Branches"
        return templates.TemplateResponse(
            request=request,
            name="report_print.html",
            context={
                "logs": processed_logs,
                "generated_at": datetime.now(),
                "start_date": actual_start,
                "end_date": actual_end,
                "branch_name": branch_name_label,
                "total_count": len(processed_logs),
                "in_count": in_count,
                "out_count": out_count,
            }
        )

    async def generate_csv():
        yield "Employee ID,Employee Name,Timestamp (Local),Punch Type,Latitude,Longitude,Branch Location,ADMS Status\n"
        for log in processed_logs:
            local_time_str = log.timestamp.isoformat() if log.timestamp else ""
            yield f'"{log.employee_id}","{log.employee_name}","{local_time_str}","{log.punch_type}",{log.latitude},{log.longitude},"{log.resolved_branch_name}","{log.adms_status}"\n'

    filename_scope = f"_branch_{branch_id}" if branch_id else ""
    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=attendance_report{filename_scope}.csv"},
    )
