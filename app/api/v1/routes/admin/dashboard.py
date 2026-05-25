import traceback
import datetime
from datetime import datetime as dt_class, timedelta
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from app.database.models import (
    PunchLog, DeviceBinding, ADMSTarget, Branch, ApiKey, AppConfig,
    BindingBranch, Employee, AdminUser, PunchType, EmployeeSupervisor
)
from app.services.auth_ui import get_current_admin
from app.services.adms_service import get_adms_config
from app.api.v1.routes.admin.base import templates, get_db

router = APIRouter(tags=["Admin UI - Dashboard"])

@router.get("/ui", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_root(
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Main admin dashboard."""
    try:
        punch_count = db.query(PunchLog).count()
        device_count = db.query(DeviceBinding).count()
        uploaded_count = db.query(PunchLog).filter(PunchLog.adms_status == "uploaded").count()
        failed_count = db.query(PunchLog).filter(PunchLog.adms_status == "failed").count()
        pending_count = db.query(PunchLog).filter(PunchLog.adms_status == "pending").count()

        logs_raw = db.query(PunchLog, Employee.full_name).\
            outerjoin(Employee, PunchLog.employee_id == Employee.employee_id).\
            order_by(PunchLog.timestamp.desc()).limit(20).all()

        logs = []
        for log, name in logs_raw:
            log.employee_name = name or "Unknown"
            logs.append(log)

        devices_raw = db.query(DeviceBinding, Employee.full_name, ApiKey.label).\
            outerjoin(Employee, DeviceBinding.employee_id == Employee.employee_id).\
            outerjoin(ApiKey, DeviceBinding.api_key_id == ApiKey.id).all()

        device_ids = [d.id for d, _, _ in devices_raw]
        
        # 1. Batch query all multi-branch assignments and branch details in one join query
        branch_assignments_all = db.query(BindingBranch, Branch).\
            join(Branch, BindingBranch.branch_id == Branch.id).\
            filter(BindingBranch.binding_id.in_(device_ids)).all() if device_ids else []
            
        from collections import defaultdict
        assignments_by_device = defaultdict(list)
        for ba, branch in branch_assignments_all:
            assignments_by_device[ba.binding_id].append({
                "id": branch.id,
                "name": branch.name,
                "binding_branch_id": ba.id
            })

        # 2. Batch query for fallback device.branch_id values
        needed_branch_ids = {d.branch_id for d, _, _ in devices_raw if d.branch_id}
        branches_by_id = {}
        if needed_branch_ids:
            all_needed_branches = db.query(Branch).filter(Branch.id.in_(needed_branch_ids)).all()
            branches_by_id = {b.id: b for b in all_needed_branches}

        devices = []
        for device, emp_name, key_label in devices_raw:
            device.employee_name = emp_name or "Unknown"
            device.api_key_label = key_label or "Legacy/Unknown"
            
            # Use pre-fetched branch assignments
            device.branch_list = list(assignments_by_device.get(device.id, []))
            
            # Fallback to direct device.branch_id if multi-branch list is empty
            if not device.branch_list and device.branch_id:
                branch = branches_by_id.get(device.branch_id)
                if branch:
                    device.branch_list.append({"id": branch.id, "name": branch.name, "binding_branch_id": None})
            devices.append(device)

        server_url, sn, device_name = get_adms_config()
        db_target = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
        timezone_offset = db_target.timezone_offset if db_target else 7

        branches = db.query(Branch).all()

        max_devices_cfg = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
        max_devices = int(max_devices_cfg.value) if max_devices_cfg else 5

        today_start = dt_class.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_in = db.query(PunchLog).filter(
            PunchLog.timestamp >= today_start,
            PunchLog.punch_type.ilike('%in%'),
        ).count()
        today_out = db.query(PunchLog).filter(
            PunchLog.timestamp >= today_start,
            PunchLog.punch_type.ilike('%out%'),
        ).count()

        # 3. Batch query the active device counts grouped by employee_id
        employee_ids = [d.employee_id for d in devices if d.employee_id]
        device_counts_raw = db.query(DeviceBinding.employee_id, func.count(DeviceBinding.id)).\
            filter(
                DeviceBinding.employee_id.in_(employee_ids),
                DeviceBinding.is_active == True,
            ).group_by(DeviceBinding.employee_id).all() if employee_ids else []
        
        employee_device_counts = {emp_id: count for emp_id, count in device_counts_raw}
        
        for d in devices:
            d.device_count_for_employee = employee_device_counts.get(d.employee_id, 0) if d.employee_id else 0

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "punch_count": punch_count,
                "device_count": device_count,
                "uploaded_count": uploaded_count,
                "failed_count": failed_count,
                "pending_count": pending_count,
                "logs": logs,
                "devices": devices,
                "server_url": server_url,
                "sn": sn,
                "device_name": device_name,
                "timezone_offset": timezone_offset,
                "branches": branches,
                "admin": admin,
                "max_devices": max_devices,
                "today_in": today_in,
                "today_out": today_out,
            },
        )
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error in Dashboard:</h3><pre>{traceback.format_exc()}</pre>", status_code=500)

@router.get("/ui/help")
async def help_page(
    request: Request,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request=request,
        name="help.html",
        context={
            "request": request,
            "app_settings": {"max_devices_per_employee": 5},
        },
    )

@router.get("/ui/analytics")
async def get_analytics_dashboard(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    # 1. Headcount
    active_headcount = db.query(Employee).filter(Employee.is_deleted == False, Employee.is_active == True).count()

    # Today's local date range (GMT+7 default)
    now_local = datetime.datetime.utcnow() + timedelta(hours=7)
    today_start_local = datetime.datetime.combine(now_local.date(), datetime.time.min)
    today_end_local = datetime.datetime.combine(now_local.date(), datetime.time.max)

    # Convert local bounds to UTC for DB
    today_start_utc = today_start_local - timedelta(hours=7)
    today_end_utc = today_end_local - timedelta(hours=7)

    # Today's unique check-ins
    today_present = db.query(func.count(func.distinct(PunchLog.employee_id))).filter(
        PunchLog.timestamp >= today_start_utc,
        PunchLog.timestamp <= today_end_utc,
        func.lower(PunchLog.punch_type).in_(["in", "check in"])
    ).scalar() or 0

    attendance_rate = 0.0
    if active_headcount > 0:
        attendance_rate = round((today_present / active_headcount) * 100, 1)

    # Total punches today
    today_punches = db.query(PunchLog).filter(
        PunchLog.timestamp >= today_start_utc,
        PunchLog.timestamp <= today_end_utc
    ).count()

    # Today's mock locations count
    today_mocks = db.query(PunchLog).filter(
        PunchLog.timestamp >= today_start_utc,
        PunchLog.timestamp <= today_end_utc,
        PunchLog.is_mock_location == True
    ).count()

    # 2. Seven-Day Trend
    trend_dates = []
    trend_rates = []
    trend_presents = []
    
    for i in range(6, -1, -1):
        day = now_local.date() - timedelta(days=i)
        day_start_utc = datetime.datetime.combine(day, datetime.time.min) - timedelta(hours=7)
        day_end_utc = datetime.datetime.combine(day, datetime.time.max) - timedelta(hours=7)
        
        day_present = db.query(func.count(func.distinct(PunchLog.employee_id))).filter(
            PunchLog.timestamp >= day_start_utc,
            PunchLog.timestamp <= day_end_utc,
            func.lower(PunchLog.punch_type).in_(["in", "check in"])
        ).scalar() or 0
        
        day_rate = 0.0
        if active_headcount > 0:
            day_rate = round((day_present / active_headcount) * 100, 1)
            
        trend_dates.append(day.strftime("%a %d %b"))
        trend_rates.append(day_rate)
        trend_presents.append(day_present)

    # 3. Branch breakdown & Hourly distribution in last 30 days
    days_30_ago = datetime.datetime.utcnow() - timedelta(days=30)
    
    branch_stats = {}
    branches = db.query(Branch).filter(Branch.is_active == True).all()
    for b in branches:
        branch_stats[b.name] = 0

    # Get employee to branch mappings
    emp_branches = db.query(Employee.employee_id, Branch.name).select_from(Employee).join(
        DeviceBinding, Employee.employee_id == DeviceBinding.employee_id
    ).join(
        BindingBranch, DeviceBinding.id == BindingBranch.binding_id
    ).join(
        Branch, BindingBranch.branch_id == Branch.id
    ).all()
    
    emp_to_branch = {eb[0]: eb[1] for eb in emp_branches}
    
    punches_30 = db.query(PunchLog.employee_id, PunchLog.timestamp).filter(
        PunchLog.timestamp >= days_30_ago
    ).all()
    
    hourly_distribution = [0] * 24
    for p in punches_30:
        br_name = emp_to_branch.get(p.employee_id)
        if br_name and br_name in branch_stats:
            branch_stats[br_name] += 1
            
        local_time = p.timestamp + timedelta(hours=7)
        hourly_distribution[local_time.hour] += 1

    # 4. Security warnings / Anomalies (last 30 days)
    anomaly_logs = db.query(PunchLog).filter(
        PunchLog.timestamp >= days_30_ago,
        or_(
            PunchLog.is_mock_location == True,
            PunchLog.notes.like("%off-site%"),
            PunchLog.notes.like("%geofence%")
        )
    ).order_by(PunchLog.timestamp.desc()).limit(15).all()

    anomalies_list = []
    for log in anomaly_logs:
        emp = db.query(Employee).filter(Employee.employee_id == log.employee_id).first()
        emp_name = emp.full_name if emp else "Unknown"
        
        branch_name = "Unknown Branch"
        if emp:
            binding = db.query(DeviceBinding).filter(DeviceBinding.employee_id == emp.employee_id, DeviceBinding.is_active == True).first()
            if binding:
                ba = db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).first()
                if ba:
                    br = db.query(Branch).filter(Branch.id == ba.branch_id).first()
                    if br:
                        branch_name = br.name
        
        anomaly_type = "Mock Location" if log.is_mock_location else "Geofence Violation"
        details = log.notes or "Coordinates outside authorized boundaries"
        if log.is_mock_location:
            details = "Mock GPS application detected on device"
            
        anomalies_list.append({
            "timestamp": log.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "employee_id": log.employee_id,
            "employee_name": emp_name,
            "branch_name": branch_name,
            "anomaly_type": anomaly_type,
            "details": details,
            "coordinates": f"{log.latitude:.5f}, {log.longitude:.5f}" if log.latitude is not None and log.longitude is not None else "0.00000, 0.00000"
        })

    return {
        "headcount": active_headcount,
        "today_present": today_present,
        "today_punches": today_punches,
        "attendance_rate": attendance_rate,
        "today_mocks": today_mocks,
        "weekly_trend": {
            "dates": trend_dates,
            "rates": trend_rates,
            "presents": trend_presents
        },
        "branch_comparison": {
            "labels": list(branch_stats.keys()),
            "data": list(branch_stats.values())
        },
        "peak_hours": {
            "labels": [f"{h:02d}:00" for h in range(24)],
            "data": hourly_distribution
        },
        "anomalies": anomalies_list
    }
