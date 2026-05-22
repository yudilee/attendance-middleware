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
from sqlalchemy import func
from pydantic import BaseModel

from app.database.models import (
    SessionLocal, DeviceBinding, PunchLog, ADMSTarget, Branch,
    BranchCheckpoint,
    ApiKey, ADMSRegisteredEmployee, AdminUser, PunchType,
    Employee, AppConfig, ADMSCredential, BindingBranch,
    EmployeeSupervisor, AttendanceCorrection,
)
from app.services.auth import verify_api_key, generate_api_key, hash_api_key
from app.services.auth_ui import (
    get_password_hash, verify_password, create_access_token,
    get_current_admin,
)
from app.services.adms_scraper import sync_employees_from_adms
from app.services.adms_service import get_adms_config, test_adms_connection, _handshake_state
from app.api.v1.schemas import (
    ADMSConfigRequest, ADMSCredentialPayload, PunchTypeResponse,
    PunchTypePayload, BranchRequest, AppConfigRequest,
    ProfileUpdateRequest, CreateUserRequest, DeviceLabelRequest,
    CorrectionRequest, CorrectionReview, SupervisorAssignment,
    OnboardGenerateRequest, CheckpointCreate, CheckpointUpdate,
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
            "qr_code_enabled": b.qr_code_enabled,
            "qr_code_data": b.qr_code_data if b.qr_code_enabled else None,
            "nfc_enabled": b.nfc_enabled,
            "nfc_tag_data": b.nfc_tag_data if b.nfc_enabled else None,
            "device_count": device_count,
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
        qr_code_enabled=req.qr_code_enabled,
        qr_code_data=req.qr_code_data if req.qr_code_enabled else None,
        nfc_enabled=req.nfc_enabled,
        nfc_tag_data=req.nfc_tag_data if req.nfc_enabled else None,
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
    branch.qr_code_enabled = req.qr_code_enabled
    branch.qr_code_data = req.qr_code_data if req.qr_code_enabled else None
    branch.nfc_enabled = req.nfc_enabled
    branch.nfc_tag_data = req.nfc_tag_data if req.nfc_enabled else None
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
        "total_employees": db.query(Employee).count(),
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
    return {"count": db.query(Employee).count()}


@router.get("/ui/employees/list")
async def list_employees(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    emps = db.query(Employee).order_by(Employee.full_name).all()
    return [{"id": e.employee_id, "name": e.full_name, "dept": e.department} for e in emps]


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
