"""
Admin UI routes (Jinja2 templates).
All routes in this module render HTML pages for the admin dashboard.
"""
import structlog
from datetime import datetime, timedelta
from typing import Optional

from fastapi import (
    APIRouter, Depends, HTTPException, Request, Response, Form,
    BackgroundTasks,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, and_
from pydantic import BaseModel

from app.database.models import (
    SessionLocal, DeviceBinding, PunchLog, ADMSTarget, Branch,
    BranchCheckpoint,
    ApiKey, ADMSRegisteredEmployee, AdminUser, PunchType,
    Employee, AppConfig, ADMSCredential, BindingBranch,
    EmployeeSupervisor, AttendanceCorrection,
    Company, EmployeeGroup, ShiftSchedule, Holiday, LeaveRequest, AuditLog,
)
from app.services.auth import verify_api_key, generate_api_key, hash_api_key
from app.services.auth_ui import (
    get_password_hash, verify_password, create_access_token,
    get_current_admin,
)
from app.services.adms_scraper import sync_employees_from_adms
from app.services.adms_service import get_adms_config, test_adms_connection, _handshake_state, delete_employee_from_adms
from app.api.v1.schemas import (
    ADMSConfigRequest, ADMSCredentialPayload, PunchTypeResponse,
    PunchTypePayload, BranchRequest, AppConfigRequest,
    ProfileUpdateRequest, CreateUserRequest, DeviceLabelRequest,
    CorrectionRequest, CorrectionReview, SupervisorAssignment,
    OnboardGenerateRequest, CheckpointCreate, CheckpointUpdate,
    EmployeeCreatePayload, EmployeeUpdatePayload, EmployeeResponse,
    ShiftScheduleCreate, ShiftScheduleUpdate, ShiftScheduleResponse,
    CompanyCreate, CompanyUpdate, CompanyResponse,
    EmployeeGroupCreate, EmployeeGroupUpdate, EmployeeGroupResponse,
    HolidayCreate, HolidayResponse,
    LeaveRequestCreate, LeaveRequestUpdate, LeaveRequestResponse,
    AuditLogResponse, SmtpSettingsRequest, SmtpSettingsResponse,
)
from app.cache import invalidate_cache

logger = structlog.get_logger()

# ARQ pool reference (set during app lifespan)
arq_pool = None

router = APIRouter(tags=["Admin UI"])

# Templates
import os
template_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "templates")
templates = Jinja2Templates(directory=template_path)


def configure(arq_pool_ref):
    """Set the ARQ pool reference from the main app."""
    global arq_pool
    arq_pool = arq_pool_ref


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ═══════════════════ AUTH ROUTES ═══════════════════


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={})


@router.post("/login")
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request=request, name="login.html", context={"error": "Invalid username or password"}
        )

    access_token = create_access_token(data={"sub": user.username})
    redirect_response = RedirectResponse(url="/", status_code=302)
    redirect_response.set_cookie(
        key="dashboard_session", value=access_token,
        httponly=True, max_age=86400, samesite="lax",
    )
    return redirect_response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("dashboard_session")
    return response


# ═══════════════════ DASHBOARD ═══════════════════


@router.get("/ui", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_root(
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Main admin dashboard."""
    import traceback
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

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
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


# ═══════════════════ ADMS SETTINGS ═══════════════════


@router.post("/ui/settings")
async def update_settings(
    config: ADMSConfigRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    target = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
    if not target:
        target = ADMSTarget()
        db.add(target)

    target.server_url = config.server_url
    target.serial_number = config.serial_number
    target.device_name = config.device_name
    target.timezone_offset = config.timezone_offset
    db.commit()
    logger.info(f"ADMS Config updated: {config.server_url} SN={config.serial_number}")
    return {"status": "success"}


@router.post("/ui/test-connection")
async def ui_test_connection(
    config: ADMSConfigRequest,
    admin: AdminUser = Depends(get_current_admin),
):
    success, message = await test_adms_connection(config.server_url, config.serial_number, config.device_name)
    return {"success": success, "message": message}


# ═══════════════════ APP CONFIG ═══════════════════


@router.get("/ui/app-settings")
async def get_app_settings(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    max_devices = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    return {"max_devices_per_employee": int(max_devices.value) if max_devices else 5}


@router.post("/ui/app-settings")
async def update_app_settings(
    config: AppConfigRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    entry = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    if entry:
        entry.value = str(config.max_devices_per_employee)
    else:
        db.add(AppConfig(
            key="max_devices_per_employee",
            value=str(config.max_devices_per_employee),
            description="Maximum number of devices an employee can register",
        ))
    db.commit()
    return {"status": "success"}


@router.get("/ui/app-settings/smtp", response_model=SmtpSettingsResponse)
async def get_smtp_settings(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    def get_config_val(key, default=""):
        cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
        return cfg.value if cfg else default

    return SmtpSettingsResponse(
        smtp_host=get_config_val("smtp_host", ""),
        smtp_port=int(get_config_val("smtp_port", "587")),
        smtp_user=get_config_val("smtp_user", ""),
        smtp_password_set=bool(get_config_val("smtp_password", "")),
        hr_email_recipients=get_config_val("hr_email_recipients", "")
    )


@router.post("/ui/app-settings/smtp")
async def update_smtp_settings(
    config: SmtpSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    def set_config_val(key, val, description=None):
        entry = db.query(AppConfig).filter(AppConfig.key == key).first()
        if entry:
            entry.value = str(val)
        else:
            db.add(AppConfig(
                key=key,
                value=str(val),
                description=description
            ))

    set_config_val("smtp_host", config.smtp_host, "HR report SMTP host server")
    set_config_val("smtp_port", config.smtp_port, "HR report SMTP server port")
    set_config_val("smtp_user", config.smtp_user, "HR report SMTP account user")
    set_config_val("hr_email_recipients", config.hr_email_recipients, "HR report email recipients (comma separated)")

    if config.smtp_password and config.smtp_password != "__UNCHANGED__":
        set_config_val("smtp_password", config.smtp_password, "HR report SMTP account password")

    db.commit()

    log_audit_action(
        db=db,
        admin_username=admin.username,
        action="update_smtp_settings",
        target_type="AppConfig",
        details=f"SMTP configurations updated (host: {config.smtp_host}, user: {config.smtp_user})",
        request=request
    )

    return {"status": "success"}



# ═══════════════════ ADMIN PROFILE ═══════════════════


@router.post("/ui/profile")
async def update_admin_profile(
    req: ProfileUpdateRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    if req.username != admin.username:
        raise HTTPException(status_code=403, detail="Cannot change another user's profile")
    admin.hashed_password = get_password_hash(req.new_password)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return {"status": "success"}


# ═══════════════════ USER MANAGEMENT ═══════════════════


@router.get("/ui/users")
async def list_users(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    users = db.query(AdminUser).order_by(AdminUser.created_at.asc()).all()
    return [{"id": u.id, "username": u.username, "role": u.role or "admin", "created_at": u.created_at} for u in users]


@router.post("/ui/users")
async def create_user(
    req: CreateUserRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    if getattr(admin, "role", "admin") not in ["superadmin", "admin"]:
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can create users")

    existing = db.query(AdminUser).filter(AdminUser.username == req.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    if not req.password or len(req.password) < 5:
        raise HTTPException(status_code=400, detail="Password is required and must be at least 5 characters")

    new_user = AdminUser(
        username=req.username,
        hashed_password=get_password_hash(req.password),
        role=req.role or "admin",
    )
    db.add(new_user)
    db.commit()
    return {"status": "success"}


@router.put("/ui/users/{user_id}")
async def update_user(
    user_id: int,
    req: CreateUserRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    if getattr(admin, "role", "admin") not in ["superadmin", "admin"]:
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can update users")

    target_user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    if target_user.username == "admin":
        if req.username != "admin":
            raise HTTPException(status_code=400, detail="Cannot rename the root admin user")
        if req.role and req.role != "superadmin":
            raise HTTPException(status_code=400, detail="Root admin must remain superadmin")

    if req.username != target_user.username:
        existing = db.query(AdminUser).filter(AdminUser.username == req.username).first()
        if existing:
            raise HTTPException(status_code=400, detail="Username already exists")
        target_user.username = req.username

    if req.password:
        if len(req.password) < 5:
            raise HTTPException(status_code=400, detail="Password must be at least 5 characters")
        target_user.hashed_password = get_password_hash(req.password)

    if req.role:
        target_user.role = req.role

    db.commit()
    return {"status": "success"}


@router.delete("/ui/users/{user_id}")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    if getattr(admin, "role", "admin") not in ["superadmin", "admin"]:
        raise HTTPException(status_code=403, detail="Only Super Admins and Admins can delete users")

    user_to_delete = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user_to_delete:
        raise HTTPException(status_code=404, detail="User not found")
    if user_to_delete.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the root admin user")
    if user_to_delete.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    db.delete(user_to_delete)
    db.commit()
    return {"status": "success"}


# ═══════════════════ DEVICE MANAGEMENT ═══════════════════


@router.post("/ui/devices/{binding_id}/approve")
async def approve_device(
    binding_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.registration_status = "approved"
    binding.approved_at = datetime.utcnow()
    binding.approved_by = admin.username
    db.commit()
    await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    return {"status": "approved"}


@router.post("/ui/devices/{binding_id}/suspend")
async def suspend_device(
    binding_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.registration_status = "suspended"
    db.commit()
    await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    return {"status": "suspended"}


@router.put("/ui/devices/{binding_id}/label")
async def update_device_label(
    binding_id: int,
    req: DeviceLabelRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.device_label = req.label
    binding.notes = req.notes
    db.commit()
    await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    return {"status": "updated"}


@router.post("/ui/devices/{binding_id}/set-active")
async def set_active_device(
    binding_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    if binding.employee_id:
        db.query(DeviceBinding).filter(
            DeviceBinding.employee_id == binding.employee_id,
            DeviceBinding.id != binding_id,
        ).update({"is_active": False})
    binding.is_active = True
    db.commit()
    await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    if binding.employee_id:
        other_bindings = db.query(DeviceBinding).filter(
            DeviceBinding.employee_id == binding.employee_id,
            DeviceBinding.id != binding_id,
        ).all()
        for ob in other_bindings:
            await invalidate_cache(f"device_config:{ob.api_key_id}:{ob.device_uuid}")
    return {"status": "updated"}


@router.delete("/ui/devices/{binding_id}/unbind")
async def unbind_device(
    binding_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if binding:
        await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
        db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).delete()
        db.delete(binding)
        db.commit()
    return {"status": "success"}


@router.post("/ui/devices/{binding_id}/bind-branch")
async def bind_device_to_branch(
    binding_id: int,
    req: dict,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    branch_id = req.get("branch_id")
    if branch_id == "" or branch_id is None:
        binding.branch_id = None
        db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).delete()
    else:
        binding.branch_id = int(branch_id)
        existing = db.query(BindingBranch).filter(
            BindingBranch.binding_id == binding.id,
            BindingBranch.branch_id == int(branch_id),
        ).first()
        if not existing:
            db.add(BindingBranch(binding_id=binding.id, branch_id=int(branch_id)))
    db.commit()
    await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    return {"status": "success"}


@router.post("/ui/devices/{binding_id}/assign")
async def assign_device_employee(
    binding_id: int,
    employee_id: str,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    max_cfg = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    max_devices = int(max_cfg.value) if max_cfg else 5
    existing_count = db.query(DeviceBinding).filter(
        DeviceBinding.employee_id == employee_id,
        DeviceBinding.is_active == True,
        DeviceBinding.registration_status.in_(["approved", "active"]),
    ).count()
    if existing_count >= max_devices:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum devices reached ({existing_count}/{max_devices}) for this employee.",
        )
    binding.employee_id = employee_id
    binding.is_active = True
    db.commit()
    await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    return {"status": "success"}


# ═══════════════════ BRANCH MANAGEMENT ═══════════════════


@router.get("/ui/devices/{binding_id}/branches")
async def get_device_branches(
    binding_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    assignments = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding_id,
    ).all()
    result = []
    for ba in assignments:
        branch = db.query(Branch).filter(Branch.id == ba.branch_id).first()
        if branch:
            result.append({
                "binding_branch_id": ba.id,
                "branch_id": branch.id,
                "branch_name": branch.name,
                "latitude": branch.latitude,
                "longitude": branch.longitude,
                "radius_meters": branch.radius_meters,
                "is_active": branch.is_active,
                "assigned_at": ba.assigned_at.isoformat() if ba.assigned_at else None,
            })
    return result


@router.post("/ui/devices/{binding_id}/branches/{branch_id}")
async def assign_branch_to_device(
    binding_id: int,
    branch_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device binding not found")
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
        
    # Validate company alignment if both employee and branch have companies assigned
    if binding.employee_id:
        employee = db.query(Employee).filter(Employee.employee_id == binding.employee_id).first()
        print(f"DEBUG EMP ID: {binding.employee_id}, FOUND EMP: {employee}, EMP COMP: {employee.company_id if employee else 'NONE'}, BRANCH COMP: {branch.company_id}")
        if employee and employee.company_id is not None and branch.company_id is not None:
            if employee.company_id != branch.company_id:
                raise HTTPException(
                    status_code=400, 
                    detail="Company mismatch: Cannot assign employee to a branch belonging to a different company."
                )

    existing = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding_id,
        BindingBranch.branch_id == branch_id,
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Branch already assigned to this device")
    db.add(BindingBranch(binding_id=binding_id, branch_id=branch_id))
    db.commit()
    await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    return {"status": "success"}


@router.delete("/ui/devices/{binding_id}/branches/{branch_id}")
async def remove_branch_from_device(
    binding_id: int,
    branch_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    assignment = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding_id,
        BindingBranch.branch_id == branch_id,
    ).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Branch not assigned to this device")
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    db.delete(assignment)
    db.commit()
    if binding:
        await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    return {"status": "success"}


@router.get("/ui/branches")
async def get_branches(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    branches = db.query(Branch).all()
    result = []
    for b in branches:
        direct_ids = {r[0] for r in db.query(DeviceBinding.id).filter(DeviceBinding.branch_id == b.id).all()}
        m2m_ids = {r[0] for r in db.query(BindingBranch.binding_id).filter(BindingBranch.branch_id == b.id).all()}
        device_count = len(direct_ids | m2m_ids)
        result.append({
            "id": b.id,
            "name": b.name,
            "latitude": b.latitude,
            "longitude": b.longitude,
            "radius_meters": b.radius_meters,
            "is_active": b.is_active,
            "geofence_type": getattr(b, "geofence_type", "circle") or "circle",
            "polygon_coordinates": getattr(b, "polygon_coordinates", None),
            "qr_code_enabled": b.qr_code_enabled,
            "qr_code_data": b.qr_code_data if b.qr_code_enabled else None,
            "nfc_enabled": b.nfc_enabled,
            "nfc_tag_data": b.nfc_tag_data if b.nfc_enabled else None,
            "device_count": device_count,
            "company_id": getattr(b, "company_id", None),
            "company_name": b.company.name if b.company else None,
            "company_code": b.company.code if b.company else None,
            "shift_schedule_id": getattr(b, "shift_schedule_id", None),
            "timezone_offset": getattr(b, "timezone_offset", 7),
        })
    return result


@router.post("/ui/branches")
async def create_branch(
    req: BranchRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    new_branch = Branch(
        name=req.name,
        latitude=req.latitude,
        longitude=req.longitude,
        radius_meters=req.radius_meters,
        is_active=True,
        geofence_type=req.geofence_type or "circle",
        polygon_coordinates=req.polygon_coordinates,
        qr_code_enabled=req.qr_code_enabled,
        qr_code_data=req.qr_code_data if req.qr_code_enabled else None,
        nfc_enabled=req.nfc_enabled,
        nfc_tag_data=req.nfc_tag_data if req.nfc_enabled else None,
        company_id=req.company_id,
        shift_schedule_id=req.shift_schedule_id,
        timezone_offset=req.timezone_offset,
    )
    db.add(new_branch)
    db.commit()
    db.refresh(new_branch)
    return {"status": "success", "id": new_branch.id}


@router.put("/ui/branches/{branch_id}")
async def update_branch(
    branch_id: int,
    req: BranchRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    branch.name = req.name
    branch.latitude = req.latitude
    branch.longitude = req.longitude
    branch.radius_meters = req.radius_meters
    branch.geofence_type = req.geofence_type or "circle"
    branch.polygon_coordinates = req.polygon_coordinates
    branch.qr_code_enabled = req.qr_code_enabled
    branch.qr_code_data = req.qr_code_data if req.qr_code_enabled else None
    branch.nfc_enabled = req.nfc_enabled
    branch.nfc_tag_data = req.nfc_tag_data if req.nfc_enabled else None
    branch.company_id = req.company_id
    branch.shift_schedule_id = req.shift_schedule_id
    branch.timezone_offset = req.timezone_offset
    db.commit()
    affected_bindings = db.query(DeviceBinding).outerjoin(
        BindingBranch, DeviceBinding.id == BindingBranch.binding_id
    ).filter(
        (DeviceBinding.branch_id == branch_id) | (BindingBranch.branch_id == branch_id)
    ).all()
    for ab in affected_bindings:
        await invalidate_cache(f"device_config:{ab.api_key_id}:{ab.device_uuid}")
    return {"status": "success"}


@router.delete("/ui/branches/{branch_id}")
async def delete_branch(
    branch_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    in_use_direct = db.query(DeviceBinding).filter(DeviceBinding.branch_id == branch_id).first()
    if in_use_direct:
        raise HTTPException(status_code=400, detail="Cannot delete branch while it is assigned to devices (direct binding). Remove the device assignment first.")
    in_use_m2m = db.query(BindingBranch).filter(BindingBranch.branch_id == branch_id).first()
    if in_use_m2m:
        raise HTTPException(status_code=400, detail="Cannot delete branch while devices are linked via multi-branch assignment. Remove the device-branch links first.")
    db.delete(branch)
    db.commit()
    return {"status": "success"}


@router.patch("/ui/branches/{branch_id}/toggle")
async def toggle_branch_status(
    branch_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    branch.is_active = not branch.is_active
    db.commit()
    affected_bindings = db.query(DeviceBinding).outerjoin(
        BindingBranch, DeviceBinding.id == BindingBranch.binding_id
    ).filter(
        (DeviceBinding.branch_id == branch_id) | (BindingBranch.branch_id == branch_id)
    ).all()
    for ab in affected_bindings:
        await invalidate_cache(f"device_config:{ab.api_key_id}:{ab.device_uuid}")
    return {"status": "success", "is_active": branch.is_active}


# ═══════════════════ API KEY MANAGEMENT ═══════════════════


@router.get("/ui/api-keys")
async def list_api_keys(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    result = []
    for k in keys:
        device_count = db.query(DeviceBinding).filter(DeviceBinding.api_key_id == k.id).count()
        expires_in_days = None
        expiry_status = "none"
        if k.expires_at:
            remaining = (k.expires_at - datetime.utcnow()).days
            expires_in_days = remaining
            if remaining < 0:
                expiry_status = "expired"
            elif remaining < 30:
                expiry_status = "expiring_soon"
            else:
                expiry_status = "valid"
        result.append({
            "id": k.id,
            "label": k.label,
            "key_preview": k.key_value[:12] + "...",
            "is_active": k.is_active,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "last_used_ip": k.last_used_ip,
            "expires_at": k.expires_at.isoformat() if k.expires_at else None,
            "expires_in_days": expires_in_days,
            "expiry_status": expiry_status,
            "device_count": device_count,
        })
    return result


@router.post("/ui/api-keys")
async def create_api_key(
    label: str = "Mobile Client",
    expires_in_days: Optional[int] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    expires_at = None
    if expires_in_days is not None and expires_in_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=expires_in_days)
    plain_key = generate_api_key()
    new_key = ApiKey(
        key_value=hash_api_key(plain_key),
        label=label,
        expires_at=expires_at,
    )
    db.add(new_key)
    db.commit()
    db.refresh(new_key)
    return {
        "key": plain_key,
        "label": new_key.label,
        "id": new_key.id,
        "expires_at": new_key.expires_at.isoformat() if new_key.expires_at else None,
    }


@router.put("/ui/api-keys/{key_id}")
async def rename_api_key(
    key_id: int,
    label: str = "",
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    if label.strip():
        key.label = label.strip()
        db.commit()
    return {"status": "success", "label": key.label}


@router.delete("/ui/api-keys/{key_id}")
async def revoke_api_key(
    key_id: int,
    hard: bool = False,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    if hard:
        if key.is_active:
            raise HTTPException(status_code=400, detail="Cannot permanently delete an active key. Revoke it first.")
        bindings = db.query(DeviceBinding).filter(DeviceBinding.api_key_id == key_id).all()
        if bindings:
            raise HTTPException(status_code=400, detail=f"Cannot delete: {len(bindings)} device(s) still bound to this key. Soft-revoke instead.")
        db.delete(key)
        db.commit()
        return {"status": "deleted"}
    key.is_active = False
    db.commit()
    return {"status": "revoked"}


@router.post("/ui/api-keys/{key_id}/rotate")
async def rotate_api_key(
    key_id: int,
    grace_period_days: int = 7,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    old_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not old_key:
        raise HTTPException(status_code=404, detail="API key not found")
    if not old_key.is_active:
        raise HTTPException(status_code=400, detail="Cannot rotate a revoked or expired key.")
    grace_end = datetime.utcnow() + timedelta(days=grace_period_days)
    if old_key.expires_at and old_key.expires_at < grace_end:
        pass
    else:
        old_key.expires_at = grace_end
    plain_key = generate_api_key()
    new_key = ApiKey(key_value=hash_api_key(plain_key), label=old_key.label)
    db.add(new_key)
    db.commit()
    db.refresh(new_key)
    return {
        "status": "rotated",
        "old_key_id": old_key.id,
        "old_key_label": old_key.label,
        "old_key_expires_at": old_key.expires_at.isoformat() if old_key.expires_at else None,
        "new_key": plain_key,
        "new_key_id": new_key.id,
        "new_key_label": new_key.label,
    }


# ═══════════════════ PUNCH TYPE MANAGEMENT ═══════════════════


@router.get("/ui/punch-types")
async def get_ui_punch_types(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    types = db.query(PunchType).order_by(PunchType.display_order).all()
    return [
        {"code": t.code, "label": t.label, "adms_status_code": t.adms_status_code,
         "display_order": t.display_order, "icon": t.icon, "color_hex": t.color_hex,
         "requires_geofence": t.requires_geofence}
        for t in types
    ]


@router.post("/ui/punch-types")
async def create_punch_type(
    payload: PunchTypePayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    existing = db.query(PunchType).filter(PunchType.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Code already exists")
    pt = PunchType(**payload.model_dump())
    db.add(pt)
    db.commit()
    await invalidate_cache("punch_types:*")
    return {"status": "created"}


@router.put("/ui/punch-types/{code}")
async def update_punch_type(
    code: str,
    payload: PunchTypePayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    pt = db.query(PunchType).filter(PunchType.code == code).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Punch type not found")
    for key, value in payload.model_dump().items():
        setattr(pt, key, value)
    db.commit()
    await invalidate_cache("punch_types:*")
    return {"status": "updated"}


@router.delete("/ui/punch-types/{code}")
async def delete_punch_type(
    code: str,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    pt = db.query(PunchType).filter(PunchType.code == code).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Punch type not found")
    db.delete(pt)
    db.commit()
    await invalidate_cache("punch_types:*")
    return {"status": "deleted"}


# ═══════════════════ ADMS SYNC ═══════════════════


@router.get("/ui/adms-credentials")
async def get_adms_credentials(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    creds = db.query(ADMSCredential).filter(ADMSCredential.is_active == True).first()
    if not creds:
        return {"url": "", "username": "", "password": ""}
    return {"url": creds.url, "username": creds.username, "password": creds.password}


@router.post("/ui/adms-credentials")
async def save_adms_credentials(
    payload: ADMSCredentialPayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    creds = db.query(ADMSCredential).filter(ADMSCredential.is_active == True).first()
    if creds:
        creds.url = payload.url
        creds.username = payload.username
        creds.password = payload.password
    else:
        creds = ADMSCredential(url=payload.url, username=payload.username, password=payload.password)
        db.add(creds)
    db.commit()
    return {"status": "success", "message": "ADMS credentials saved."}


@router.post("/ui/adms-sync")
async def trigger_adms_sync(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    if arq_pool:
        try:
            job = await arq_pool.enqueue_job("retry_failed_punches")
            return {"status": "triggered", "job_id": job.job_id}
        except Exception as e:
            logger.warning("adms_retry_enqueue_failed", error=str(e))
    success, msg = sync_employees_from_adms(db)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}


@router.get("/ui/adms-sync-info")
async def get_adms_sync_info(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    def get_val(key, default="Never"):
        cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
        return cfg.value if cfg else default
    return {
        "last_sync": get_val("last_adms_sync_time"),
        "last_count": get_val("last_adms_sync_count"),
        "last_status": get_val("last_adms_sync_status"),
        "total_employees": db.query(Employee).filter(Employee.is_deleted == False).count(),
        "heartbeat_connected": get_val("adms_connected", "false") == "true",
        "heartbeat_last_contact": get_val("adms_last_contact", ""),
        "heartbeat_last_error": get_val("adms_last_error", ""),
    }


@router.get("/ui/adms-sync-status")
async def get_adms_sync_status(
    request: Request,
    db: Session = Depends(get_db),
    current_user: AdminUser = Depends(get_current_admin),
):
    total = db.query(PunchLog).count()
    synced = db.query(PunchLog).filter(PunchLog.server_sync_status == "synced").count()
    pending = db.query(PunchLog).filter(PunchLog.server_sync_status == "pending").count()
    failed = db.query(PunchLog).filter(PunchLog.server_sync_status == "failed").count()

    recent_failures = db.query(PunchLog).filter(
        PunchLog.server_sync_status == "failed",
    ).order_by(PunchLog.timestamp.desc()).limit(50).all()

    last_24h = datetime.utcnow() - timedelta(hours=24)
    synced_24h = db.query(PunchLog).filter(
        PunchLog.server_sync_status == "synced",
        PunchLog.synced_at >= last_24h,
    ).count()

    def get_cfg(key: str, default: str = "") -> str:
        cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
        return cfg.value if cfg else default

    adms_connected = get_cfg("adms_connected", "false") == "true"
    raw_lc = get_cfg("adms_last_contact", "")
    adms_last_handshake = "Never"
    if raw_lc:
        try:
            lc_dt = datetime.fromisoformat(raw_lc)
            adms_last_handshake = lc_dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            pass
    adms_error = get_cfg("adms_last_error", "") or None
    worker_running = arq_pool is not None

    return templates.TemplateResponse(
        request=request,
        name="adms_sync.html",
        context={
            "request": request,
            "stats": {
                "total": total,
                "synced": synced,
                "pending": pending,
                "failed": failed,
                "synced_24h": synced_24h,
                "sync_rate": round((synced / total * 100), 1) if total > 0 else 0,
            },
            "recent_failures": recent_failures,
            "adms_connected": adms_connected,
            "adms_last_handshake": adms_last_handshake,
            "adms_error": adms_error,
            "worker_running": worker_running,
            "app_settings": {"max_devices_per_employee": 5},
        },
    )


@router.get("/ui/employees/count")
async def get_employee_count(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    return {"count": db.query(Employee).filter(Employee.is_deleted == False).count()}


@router.get("/ui/employees/list")
async def list_employees(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    emps = db.query(Employee).filter(Employee.is_deleted == False).order_by(Employee.full_name).all()
    return [{"id": e.employee_id, "name": e.full_name, "dept": e.department} for e in emps]


@router.get("/ui/employees", response_model=list[EmployeeResponse])
async def list_employees_detailed(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Get detailed list of employees including device count and ADMS registration status.
    Filters out soft-deleted employees.
    """
    # 1. Fetch active employees
    emps = db.query(Employee).filter(Employee.is_deleted == False).order_by(Employee.full_name).all()
    
    # 2. Count active bindings for each employee
    active_bindings = db.query(DeviceBinding.employee_id, func.count(DeviceBinding.id)).\
        filter(DeviceBinding.is_active == True).\
        group_by(DeviceBinding.employee_id).all()
    device_counts = {emp_id: count for emp_id, count in active_bindings if emp_id}

    # 3. Get ADMS registered employee list
    adms_registered = db.query(ADMSRegisteredEmployee.employee_id).all()
    adms_registered_set = {r[0] for r in adms_registered if r[0]}

    result = []
    for e in emps:
        result.append(EmployeeResponse(
            employee_id=e.employee_id,
            full_name=e.full_name,
            department=e.department,
            is_active=e.is_active,
            is_deleted=e.is_deleted,
            employee_type=e.employee_type or "regular",
            company_id=e.company_id,
            group_id=e.group_id,
            shift_schedule_id=e.shift_schedule_id,
            last_synced=e.last_synced,
            device_count=device_counts.get(e.employee_id, 0),
            adms_registered=(e.employee_id in adms_registered_set)
        ))
    return result


@router.post("/ui/employees")
async def create_employee(
    payload: EmployeeCreatePayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Create a new employee locally.
    Validates unique employee_id and runs registration on ADMS in the background.
    """
    import re
    # Validate PIN is digits only
    if not re.match(r"^\d+$", payload.employee_id):
        raise HTTPException(status_code=400, detail="Employee ID (PIN) must contain digits only.")
    
    # Check if duplicate PIN exists
    existing = db.query(Employee).filter(Employee.employee_id == payload.employee_id).first()
    if existing:
        if existing.is_deleted:
            # Re-activate soft-deleted employee if they create it again
            existing.is_deleted = False
            existing.full_name = payload.full_name
            existing.department = payload.department
            existing.is_active = payload.is_active
            existing.employee_type = payload.employee_type
            existing.company_id = payload.company_id
            existing.group_id = payload.group_id
            existing.shift_schedule_id = payload.shift_schedule_id
            existing.last_synced = datetime.utcnow()
            db.commit()
            db.refresh(existing)
            logger.info("employee_reactivated", employee_id=payload.employee_id, admin=admin.username)
            log_audit_action(db, admin.username, "reactivated_employee", "employee", payload.employee_id, f"Reactivated employee {payload.full_name} ({payload.employee_type})")
        else:
            raise HTTPException(status_code=400, detail=f"Employee ID {payload.employee_id} already exists.")
    else:
        new_emp = Employee(
            employee_id=payload.employee_id,
            full_name=payload.full_name,
            department=payload.department,
            is_active=payload.is_active,
            employee_type=payload.employee_type,
            company_id=payload.company_id,
            group_id=payload.group_id,
            shift_schedule_id=payload.shift_schedule_id,
            is_deleted=False
        )
        db.add(new_emp)
        db.commit()
        logger.info("employee_created", employee_id=payload.employee_id, admin=admin.username)
        log_audit_action(db, admin.username, "created_employee", "employee", payload.employee_id, f"Created employee {payload.full_name} ({payload.employee_type})")

    # ── Auto-register employee on ADMS asynchronously if active and regular ──
    if payload.is_active and payload.employee_type == "regular":
        import httpx
        from app.services.adms_service import register_employee_on_adms
        server_url, sn, _ = get_adms_config()
        if server_url:
            async def _bg_reg():
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await register_employee_on_adms(client, server_url, sn, payload.employee_id, payload.full_name)
                except Exception as e:
                    logger.warning("adms_bg_registration_failed", employee_id=payload.employee_id, error=str(e))
            
            import asyncio
            asyncio.create_task(_bg_reg())

    return {"status": "success", "message": "Employee created successfully"}


@router.put("/ui/employees/{employee_id}")
async def update_employee(
    employee_id: str,
    payload: EmployeeUpdatePayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Update local employee data.
    Aligns changes with the ADMS server.
    """
    emp = db.query(Employee).filter(Employee.employee_id == employee_id, Employee.is_deleted == False).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    status_changed = False
    name_changed = False
    
    if payload.full_name is not None and payload.full_name != emp.full_name:
        emp.full_name = payload.full_name
        name_changed = True
        
    if payload.department is not None:
        emp.department = payload.department
        
    if payload.is_active is not None and payload.is_active != emp.is_active:
        emp.is_active = payload.is_active
        status_changed = True

    if payload.employee_type is not None:
        emp.employee_type = payload.employee_type
        
    if payload.company_id is not None:
        emp.company_id = payload.company_id
        
    if payload.group_id is not None:
        emp.group_id = payload.group_id
        
    if payload.shift_schedule_id is not None:
        emp.shift_schedule_id = payload.shift_schedule_id

    emp.last_synced = datetime.utcnow()
    db.commit()

    logger.info("employee_updated", employee_id=employee_id, admin=admin.username)
    log_audit_action(db, admin.username, "updated_employee", "employee", employee_id, f"Updated employee {emp.full_name} details")

    # If employee is deactivated, invalidate caches for their device bindings
    if status_changed and not emp.is_active:
        bindings = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).all()
        for binding in bindings:
            await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")

    # ── Aligns changes with ADMS asynchronously (Only for regular employees) ──
    # If the name or status changed, we update their record on the ADMS server via OPERLOG push
    if emp.employee_type == "regular" and (name_changed or (status_changed and emp.is_active)):
        import httpx
        from app.services.adms_service import register_employee_on_adms
        server_url, sn, _ = get_adms_config()
        if server_url:
            async def _bg_sync():
                try:
                    # Remove local tracker so register_employee_on_adms forces a push
                    local_db = SessionLocal()
                    try:
                        existing = local_db.query(ADMSRegisteredEmployee).filter(
                            ADMSRegisteredEmployee.employee_id == employee_id
                        ).first()
                        if existing:
                            local_db.delete(existing)
                            local_db.commit()
                    finally:
                        local_db.close()

                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await register_employee_on_adms(client, server_url, sn, employee_id, emp.full_name)
                except Exception as e:
                    logger.warning("adms_bg_sync_failed", employee_id=employee_id, error=str(e))
            
            import asyncio
            asyncio.create_task(_bg_sync())

    return {"status": "success", "message": "Employee updated successfully"}


@router.delete("/ui/employees/{employee_id}")
async def soft_delete_employee(
    employee_id: str,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Soft-deletes employee from local database (marks is_deleted = True, is_active = False).
    Cascade deletes device bindings, supervisor assignments, ADMSRegisteredEmployee records,
    and invalidates all cache. Keeps the ADMS server intact.
    """
    emp = db.query(Employee).filter(Employee.employee_id == employee_id, Employee.is_deleted == False).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 1. Soft-delete employee
    emp.is_deleted = True
    emp.is_active = False
    emp.last_synced = datetime.utcnow()

    # 2. Cascade delete device bindings and clear cache
    bindings = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).all()
    for binding in bindings:
        await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
        db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).delete()
        db.delete(binding)

    # 3. Cascade delete supervisor assignments
    db.query(EmployeeSupervisor).filter(
        (EmployeeSupervisor.supervisor_id == employee_id) |
        (EmployeeSupervisor.employee_id == employee_id)
    ).delete()

    # 4. Remove from ADMS local tracking so next sync is clean
    adms_track = db.query(ADMSRegisteredEmployee).filter(
        ADMSRegisteredEmployee.employee_id == employee_id
    ).first()
    if adms_track:
        db.delete(adms_track)

    db.commit()
    logger.info("employee_soft_deleted", employee_id=employee_id, admin=admin.username)
    log_audit_action(db, admin.username, "deleted_employee", "employee", employee_id, f"Soft-deleted employee {emp.full_name} and cascade-revoked device bindings")
    return {"status": "success", "message": f"Employee {employee_id} soft-deleted and app access cascade-revoked"}


@router.post("/ui/employees/{employee_id}/delete-from-adms")
async def delete_employee_from_adms_endpoint(
    employee_id: str,
    current_user: AdminUser = Depends(get_current_admin),
):
    """
    Delete an employee from the ADMS server.
    Sends a delete command via the ZKTeco device protocol (OPERLOG).
    Does NOT delete the employee from the local database.
    """
    import httpx

    server_url, sn, _ = get_adms_config()
    if not server_url:
        raise HTTPException(status_code=400, detail="ADMS server not configured")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        success = await delete_employee_from_adms(client, server_url, sn, employee_id)

    if success:
        logger.info(f"🗑️ Admin {current_user.username} deleted employee {employee_id} from ADMS")
        return {"status": "success", "message": f"Delete command sent for {employee_id}"}
    else:
        raise HTTPException(status_code=502, detail=f"Failed to delete {employee_id} from ADMS")


# ═══════════════════ SELFIE SERVING ═══════════════════


@router.get("/ui/selfie/{filename}")
async def get_selfie(
    filename: str,
    current_user: AdminUser = Depends(get_current_admin),
):
    import os
    upload_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "uploads", "selfies",
    )
    filepath = os.path.join(upload_dir, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Selfie not found")
    from fastapi.responses import FileResponse
    return FileResponse(filepath, media_type="image/jpeg")


# ═══════════════════ SUPERVISOR MANAGEMENT (UI) ═══════════════════


@router.post("/ui/supervisors/assign")
async def assign_supervisor(
    assignment: SupervisorAssignment,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(EmployeeSupervisor).filter(
        EmployeeSupervisor.supervisor_id == assignment.supervisor_id,
        EmployeeSupervisor.employee_id == assignment.employee_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Assignment already exists")
    mapping = EmployeeSupervisor(
        supervisor_id=assignment.supervisor_id,
        employee_id=assignment.employee_id,
    )
    db.add(mapping)
    db.commit()
    return {"status": "assigned"}


@router.delete("/ui/supervisors/assign/{mapping_id}")
async def remove_supervisor(
    mapping_id: int,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    mapping = db.query(EmployeeSupervisor).filter(EmployeeSupervisor.id == mapping_id).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Assignment not found")
    db.delete(mapping)
    db.commit()
    return {"status": "removed"}


@router.get("/ui/supervisors/list")
async def list_supervisors(
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    assignments = db.query(EmployeeSupervisor).all()
    return {
        "assignments": [
            {
                "id": a.id,
                "supervisor_id": a.supervisor_id,
                "employee_id": a.employee_id,
                "created_at": a.created_at.isoformat(),
            }
            for a in assignments
        ],
    }


@router.get("/ui/supervisors")
async def supervisor_management(
    request: Request,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "section": "supervisors",
            "admin": current_user,
            "devices": db.query(DeviceBinding).all(),
            "branches": db.query(Branch).all(),
            "api_keys": db.query(ApiKey).all(),
            "punch_types": db.query(PunchType).all(),
            "adms_targets": db.query(ADMSTarget).all(),
            "app_settings": {"max_devices_per_employee": 5},
        },
    )


# ═══════════════════ BRANCH CHECKPOINT MANAGEMENT ═══════════════════


@router.get("/ui/branches/{branch_id}/checkpoints")
async def list_checkpoints(
    branch_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """List all checkpoints for a branch."""
    checkpoints = db.query(BranchCheckpoint).filter(
        BranchCheckpoint.branch_id == branch_id,
    ).order_by(BranchCheckpoint.name).all()
    return [
        {
            "id": cp.id,
            "branch_id": cp.branch_id,
            "name": cp.name,
            "latitude": cp.latitude,
            "longitude": cp.longitude,
            "radius_meters": cp.radius_meters,
            "is_active": cp.is_active,
            "geofence_type": getattr(cp, "geofence_type", "circle") or "circle",
            "polygon_coordinates": getattr(cp, "polygon_coordinates", None),
            "created_at": cp.created_at.isoformat() if cp.created_at else None,
        }
        for cp in checkpoints
    ]


@router.post("/ui/branches/{branch_id}/checkpoints")
async def create_checkpoint(
    branch_id: int,
    req: CheckpointCreate,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Add a new clock-in checkpoint to a branch."""
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")

    cp = BranchCheckpoint(
        branch_id=branch_id,
        name=req.name,
        latitude=req.latitude,
        longitude=req.longitude,
        radius_meters=req.radius_meters,
        is_active=req.is_active,
        geofence_type=req.geofence_type or "circle",
        polygon_coordinates=req.polygon_coordinates,
    )
    db.add(cp)
    db.commit()
    db.refresh(cp)
    logger.info("checkpoint_created", branch_id=branch_id, name=req.name, admin=admin.username)
    affected_bindings = db.query(DeviceBinding).outerjoin(
        BindingBranch, DeviceBinding.id == BindingBranch.binding_id
    ).filter(
        (DeviceBinding.branch_id == branch_id) | (BindingBranch.branch_id == branch_id)
    ).all()
    for ab in affected_bindings:
        await invalidate_cache(f"device_config:{ab.api_key_id}:{ab.device_uuid}")
    return {"status": "created", "id": cp.id}


@router.put("/ui/branches/{branch_id}/checkpoints/{checkpoint_id}")
async def update_checkpoint(
    branch_id: int,
    checkpoint_id: int,
    req: CheckpointUpdate,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Update a clock-in checkpoint."""
    cp = db.query(BranchCheckpoint).filter(
        BranchCheckpoint.id == checkpoint_id,
        BranchCheckpoint.branch_id == branch_id,
    ).first()
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    if req.name is not None:
        cp.name = req.name
    if req.latitude is not None:
        cp.latitude = req.latitude
    if req.longitude is not None:
        cp.longitude = req.longitude
    if req.radius_meters is not None:
        cp.radius_meters = req.radius_meters
    if req.is_active is not None:
        cp.is_active = req.is_active
    if req.geofence_type is not None:
        cp.geofence_type = req.geofence_type
    if req.polygon_coordinates is not None:
        cp.polygon_coordinates = req.polygon_coordinates

    db.commit()
    logger.info("checkpoint_updated", checkpoint_id=checkpoint_id, admin=admin.username)
    affected_bindings = db.query(DeviceBinding).outerjoin(
        BindingBranch, DeviceBinding.id == BindingBranch.binding_id
    ).filter(
        (DeviceBinding.branch_id == branch_id) | (BindingBranch.branch_id == branch_id)
    ).all()
    for ab in affected_bindings:
        await invalidate_cache(f"device_config:{ab.api_key_id}:{ab.device_uuid}")
    return {"status": "updated"}


@router.delete("/ui/branches/{branch_id}/checkpoints/{checkpoint_id}")
async def delete_checkpoint(
    branch_id: int,
    checkpoint_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Delete a clock-in checkpoint."""
    cp = db.query(BranchCheckpoint).filter(
        BranchCheckpoint.id == checkpoint_id,
        BranchCheckpoint.branch_id == branch_id,
    ).first()
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    db.delete(cp)
    db.commit()
    logger.info("checkpoint_deleted", checkpoint_id=checkpoint_id, admin=admin.username)
    affected_bindings = db.query(DeviceBinding).outerjoin(
        BindingBranch, DeviceBinding.id == BindingBranch.binding_id
    ).filter(
        (DeviceBinding.branch_id == branch_id) | (BindingBranch.branch_id == branch_id)
    ).all()
    for ab in affected_bindings:
        await invalidate_cache(f"device_config:{ab.api_key_id}:{ab.device_uuid}")
    return {"status": "deleted"}


@router.patch("/ui/branches/{branch_id}/checkpoints/{checkpoint_id}/toggle")
async def toggle_checkpoint(
    branch_id: int,
    checkpoint_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Toggle a checkpoint's active status."""
    cp = db.query(BranchCheckpoint).filter(
        BranchCheckpoint.id == checkpoint_id,
        BranchCheckpoint.branch_id == branch_id,
    ).first()
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")

    cp.is_active = not cp.is_active
    db.commit()
    affected_bindings = db.query(DeviceBinding).outerjoin(
        BindingBranch, DeviceBinding.id == BindingBranch.binding_id
    ).filter(
        (DeviceBinding.branch_id == branch_id) | (BindingBranch.branch_id == branch_id)
    ).all()
    for ab in affected_bindings:
        await invalidate_cache(f"device_config:{ab.api_key_id}:{ab.device_uuid}")
    return {"status": "success", "is_active": cp.is_active}


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


@router.get("/ui/reports/export")
async def export_reports(
    request: Request,
    format: str = "csv",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    branch_id: Optional[int] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    from fastapi.responses import StreamingResponse
    from app.services.geo import is_within_any_fence

    # Helper to parse dates robustly
    def parse_date(date_str: str) -> Optional[datetime]:
        if not date_str:
            return None
        try:
            if len(date_str) <= 10:
                return datetime.strptime(date_str, "%Y-%m-%d")
            return datetime.fromisoformat(date_str.replace("Z", ""))
        except Exception:
            return None

    # 1. Base Query
    query = db.query(PunchLog, Employee.full_name).\
        outerjoin(Employee, PunchLog.employee_id == Employee.employee_id)

    # 2. Date Filtering
    start_dt = parse_date(start_date)
    if start_dt:
        query = query.filter(PunchLog.timestamp >= start_dt)

    end_dt = parse_date(end_date)
    if end_dt:
        if len(end_date) <= 10:
            end_dt = end_dt + timedelta(days=1) - timedelta(seconds=1)
        query = query.filter(PunchLog.timestamp <= end_dt)

    # Sort by timestamp
    query = query.order_by(PunchLog.timestamp.desc())

    # Fetch all records
    logs_raw = query.all()

    # Fetch all branches once to do geofence analysis
    branches = db.query(Branch).all()

    # Target branch for filtering
    target_branch = None
    if branch_id:
        target_branch = db.query(Branch).filter(Branch.id == branch_id).first()

    processed_logs = []
    in_count = 0
    out_count = 0

    for log, name in logs_raw:
        # Resolve branch by coordinates
        in_fence, dist, r_branch_name = is_within_any_fence(log.latitude, log.longitude, branches, db=db)
        if not in_fence:
            r_branch_name = "Off-site"

        # If branch filter is enabled, verify it belongs to that branch
        if branch_id and target_branch:
            # Check if this punch is inside target branch's geofence/checkpoints
            in_target_fence, _, _ = is_within_any_fence(log.latitude, log.longitude, [target_branch], db=db)
            if not in_target_fence:
                continue

        # Include log
        log.employee_name = name or "Unknown"
        log.resolved_branch_name = r_branch_name
        processed_logs.append(log)

        if log.punch_type.lower() == "in":
            in_count += 1
        else:
            out_count += 1

    # Format output
    if format == "print":
        branch_name_label = target_branch.name if target_branch else "All Branches"
        return templates.TemplateResponse(
            request=request,
            name="report_print.html",
            context={
                "logs": processed_logs,
                "generated_at": datetime.now(),
                "start_date": start_date,
                "end_date": end_date,
                "branch_name": branch_name_label,
                "total_count": len(processed_logs),
                "in_count": in_count,
                "out_count": out_count,
            }
        )

    # Default CSV Streaming response
    async def generate_csv():
        yield "Employee ID,Employee Name,Timestamp (Local),Punch Type,Latitude,Longitude,Branch Location,ADMS Status\n"
        for log in processed_logs:
            local_time_str = log.timestamp.isoformat() if log.timestamp else ""
            yield f'"{log.employee_id}","{log.employee_name}","{local_time_str}","{log.punch_type}",{log.latitude},{log.longitude},"{log.resolved_branch_name}","{log.adms_status}"\n'

    filename_scope = f"_branch_{branch_id}" if branch_id else ""
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=attendance_report{filename_scope}.csv"},
    )


# ═══════════════════ Audit Log Helper ═══════════════════
def log_audit_action(db: Session, admin_username: str, action: str, target_type: str = None, target_id: str = None, details: str = None, request: Request = None):
    ip_address = None
    if request:
        ip_address = request.client.host
    log_entry = AuditLog(
        admin_username=admin_username,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
        details=details,
        ip_address=ip_address
    )
    db.add(log_entry)
    db.commit()


# ═══════════════════ Company CRUD Endpoints ═══════════════════
@router.get("/ui/companies", response_model=list[CompanyResponse])
async def get_companies(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    return db.query(Company).order_by(Company.name).all()


@router.post("/ui/companies", response_model=CompanyResponse)
async def create_company(
    payload: CompanyCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    existing = db.query(Company).filter(Company.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Company code already exists.")
    
    company = Company(
        name=payload.name,
        code=payload.code,
        is_active=payload.is_active,
        shift_schedule_id=payload.shift_schedule_id
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    
    log_audit_action(db, admin.username, "created_company", "company", company.id, f"Created company {company.name} ({company.code})", request)
    return company


@router.put("/ui/companies/{company_id}", response_model=CompanyResponse)
async def update_company(
    company_id: int,
    payload: CompanyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
        
    if payload.name is not None:
        company.name = payload.name
    if payload.code is not None:
        # Check unique
        existing = db.query(Company).filter(Company.code == payload.code, Company.id != company_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Company code already exists.")
        company.code = payload.code
    if payload.is_active is not None:
        company.is_active = payload.is_active
    if payload.shift_schedule_id is not None:
        company.shift_schedule_id = payload.shift_schedule_id
        
    company.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(company)
    
    log_audit_action(db, admin.username, "updated_company", "company", company.id, f"Updated company {company.name}", request)
    return company


@router.delete("/ui/companies/{company_id}")
async def delete_company(
    company_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    
    # Check if branches depend on this company
    has_branches = db.query(Branch).filter(Branch.company_id == company_id).first()
    if has_branches:
        raise HTTPException(status_code=400, detail="Cannot delete company. It still has active branches associated.")
        
    company_name = company.name
    db.delete(company)
    db.commit()
    
    log_audit_action(db, admin.username, "deleted_company", "company", company_id, f"Deleted company {company_name}", request)
    return {"status": "success", "message": "Company deleted successfully."}


@router.get("/ui/companies/{company_id}/branches")
async def get_company_branches(
    company_id: int,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Fetch branches belonging to a specific company for cascading selectors."""
    branches = db.query(Branch).filter(Branch.company_id == company_id, Branch.is_active == True).order_by(Branch.name).all()
    return [{"id": b.id, "name": b.name} for b in branches]


# ═══════════════════ Employee Group CRUD Endpoints ═══════════════════
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


# ═══════════════════ Shift Schedule CRUD Endpoints ═══════════════════
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
    # If is_default is true, unset default from other schedules
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
        anchor_date=payload.anchor_date
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
        # Unset other defaults
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
        
    db.commit()
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
    
    log_audit_action(db, admin.username, "deleted_shift", "shift_schedule", schedule_id, f"Deleted shift schedule {schedule_name}", request)
    return {"status": "success", "message": "Shift schedule deleted successfully."}


# ═══════════════════ Holiday CRUD Endpoints ═══════════════════
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
    
    log_audit_action(db, admin.username, "deleted_holiday", "holiday", holiday_id, f"Deleted holiday {holiday_name}", request)
    return {"status": "success", "message": "Holiday deleted successfully."}


# ═══════════════════ Leave Request CRUD Endpoints ═══════════════════
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
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    # Verify employee exists
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
    
    log_audit_action(db, admin.username, "created_leave", "leave_request", leave.id, f"Created approved leave request for {leave.employee_id} ({leave.start_date} to {leave.end_date})", request)
    return leave


@router.put("/ui/leave-requests/{leave_id}", response_model=LeaveRequestResponse)
async def review_leave_request(
    leave_id: int,
    payload: LeaveRequestUpdate,
    request: Request,
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


# ═══════════════════ Audit Log Endpoints ═══════════════════
@router.get("/ui/audit-logs", response_model=list[AuditLogResponse])
async def get_audit_logs(
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    return db.query(AuditLog).order_by(AuditLog.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/ui/analytics")
async def get_analytics_dashboard(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    import datetime
    from datetime import timedelta
    from sqlalchemy import func
    from app.database.models import Employee, PunchLog, Branch, DeviceBinding, BindingBranch

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
        # Branch breakdown
        br_name = emp_to_branch.get(p.employee_id)
        if br_name and br_name in branch_stats:
            branch_stats[br_name] += 1
            
        # Peak hours local calculation
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
        
        # Determine branch
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


# ═══════════════════ Report Endpoints ═══════════════════
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
    from app.services.report_service import pair_employee_punches
    from datetime import date
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
        from app.database.models import DeviceBinding, BindingBranch
        bindings = db.query(DeviceBinding.employee_id).outerjoin(
            BindingBranch, DeviceBinding.id == BindingBranch.binding_id
        ).filter(
            or_(DeviceBinding.branch_id == branch_id, BindingBranch.branch_id == branch_id)
        ).subquery()
        query = query.filter(Employee.employee_id.in_(bindings))

    employees = query.order_by(Employee.full_name).all()
    
    results = []
    for emp in employees:
        paired = pair_employee_punches(db, emp, start_d, end_d)
        results.append(paired)
        
    return results


@router.get("/ui/reports/export")
async def export_excel_report(
    from_date: str,
    to_date: str,
    company_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    group_id: Optional[int] = None,
    department: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    from app.services.report_service import generate_excel_report
    from datetime import date
    import io
    from fastapi.responses import StreamingResponse
    try:
        start_d = date.fromisoformat(from_date)
        end_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    # Generate workbook
    wb = generate_excel_report(
        db, start_d, end_d,
        company_id=company_id,
        branch_id=branch_id,
        group_id=group_id,
        department=department
    )
    
    # Save workbook to memory buffer
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    
    log_audit_action(db, admin.username, "exported_excel_report", "report", None, f"Exported attendance report from {from_date} to {to_date}")
    
    filename = f"attendance_report_{from_date}_to_{to_date}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
