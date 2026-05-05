import asyncio
import logging
import structlog
import json
import csv
import io
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request, UploadFile, File
from fastapi.responses import StreamingResponse, HTMLResponse, RedirectResponse, FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, text
from typing import Optional
import sys
import os
import uuid

# ── Rate Limiting (slowapi) ──────────────────────────────────────────────────
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from pydantic import BaseModel
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import Response

from app.services.auth import verify_api_key, generate_api_key
from app.services.auth_ui import get_password_hash, verify_password, create_access_token, get_current_admin

from app.database.models import init_db, SessionLocal, DeviceBinding, PunchLog, ADMSTarget, Branch, ApiKey, ADMSRegisteredEmployee, AdminUser, PunchType, Employee, AppConfig, ADMSCredential, BindingBranch, EmployeeSupervisor, AttendanceCorrection
from app.cache import init_redis, close_redis, get_cache, set_cache, invalidate_cache
from app.config import settings
from app.api.v1.schemas import (
    PunchRequest, PunchResponse, DeviceConfigResponse,
    BatchPunchRequest, BatchPunchResponse, BatchPunchResult, PunchTypeResponse,
    ADMSCredentialPayload, AppStatusResponse, BranchInfo,
    CorrectionRequest, CorrectionReview, SupervisorAssignment
)
from app.services.adms_scraper import sync_employees_from_adms
from app.services.geo import is_within_fence, is_within_any_fence
from app.services.adms_service import push_to_adms, get_adms_config, test_adms_connection, _handshake_state
from app.worker import sync_punches_to_adms
import arq

# ── Structured Logging ────────────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer() if settings.env == "development"
        else structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()

# ── ARQ Pool (global, initialized at startup) ──────────────────────────────────
arq_pool: Optional[arq.ArqRedis] = None


async def adms_sync_loop():
    """Background task to sync ADMS employees daily at 2:00 AM (or whatever internal interval we set).
       For simplicity, we'll run it once every 24 hours."""
    while True:
        try:
            db = SessionLocal()
            try:
                logger.info("adms_sync_started")
                success, msg = sync_employees_from_adms(db)
                if success:
                    logger.info("adms_sync_completed", result=msg)
                else:
                    logger.warning("adms_sync_failed", reason=msg)
            finally:
                db.close()
        except Exception as e:
            logger.error("adms_sync_error", exc_info=True, error=str(e))
        
        await asyncio.sleep(86400) # Sleep 24 hours


# ─── Lifespan (replaces deprecated @app.on_event) ────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global arq_pool
    logger.info("initializing_database")
    init_db()
    
    logger.info("initializing_redis")
    try:
        await init_redis()
        logger.info("redis_connected")
    except Exception as e:
        logger.warning("redis_connection_failed", error=str(e))

    # Auto-initialize default admin if none exists
    db = SessionLocal()
    try:
        admin = db.query(AdminUser).first()
        if not admin:
            logger.info("creating_default_admin")
            default_admin = AdminUser(username="admin", hashed_password=get_password_hash("admin"))
            db.add(default_admin)
            db.commit()
    finally:
        db.close()

    # Initialize ARQ pool (replaces asyncio background tasks)
    logger.info("initializing_arq_pool")
    try:
        redis_settings = arq.connections.RedisSettings(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
        )
        arq_pool = await arq.create_pool(redis_settings)
        logger.info("arq_pool_initialized")
    except Exception as e:
        logger.warning("arq_pool_initialization_failed", error=str(e))
        arq_pool = None

    yield

    # Graceful shutdown: close ARQ pool
    logger.info("shutting_down_arq_pool")
    if arq_pool:
        arq_pool.close()
        await arq_pool.wait_closed()
    logger.info("stopping_redis")
    await close_redis()
    logger.info("adms_background_tasks_stopped")


app = FastAPI(title="Secure Geo-Fenced Attendance Aggregator", lifespan=lifespan)

# ── Rate Limiter Setup ───────────────────────────────────────────────────────
def api_key_identifier(request: Request):
    """Use X-API-Key header as the rate limit key, falling back to client IP."""
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return api_key
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.client
    if client:
        return client.host
    return "unknown"

limiter = Limiter(key_func=api_key_identifier)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Mount Static Files and Templates
static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=template_path)

# ─── Selfie Upload Directory ──────────────────────────────────────────────────
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads", "selfies")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ─── DB Dependency ────────────────────────────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── UI ROUTES ────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={})

from fastapi import Form
@app.post("/login")
async def login_submit(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(
            request=request, name="login.html", context={"error": "Invalid username or password"}
        )
    
    access_token = create_access_token(data={"sub": user.username})
    redirect_response = RedirectResponse(url="/", status_code=302)
    redirect_response.set_cookie(
        key="dashboard_session", value=access_token, httponly=True, max_age=86400, samesite="lax"
    )
    return redirect_response

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("dashboard_session")
    return response

@app.get("/ui", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
async def dashboard_root(request: Request, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    import traceback
    try:
        punch_count = db.query(PunchLog).count()
        device_count = db.query(DeviceBinding).count()
        uploaded_count = db.query(PunchLog).filter(PunchLog.adms_status == "uploaded").count()
        failed_count = db.query(PunchLog).filter(PunchLog.adms_status == "failed").count()
        pending_count = db.query(PunchLog).filter(PunchLog.adms_status == "pending").count()
        # Fetch logs with employee names
        logs_raw = db.query(PunchLog, Employee.full_name).\
            outerjoin(Employee, PunchLog.employee_id == Employee.employee_id).\
            order_by(PunchLog.timestamp.desc()).limit(20).all()
        
        # Format logs for template
        logs = []
        for log, name in logs_raw:
            log.employee_name = name or "Unknown"
            logs.append(log)

        # Fetch devices with employee names and API key labels
        devices_raw = db.query(DeviceBinding, Employee.full_name, ApiKey.label).\
            outerjoin(Employee, DeviceBinding.employee_id == Employee.employee_id).\
            outerjoin(ApiKey, DeviceBinding.api_key_id == ApiKey.id).all()
        
        devices = []
        for device, emp_name, key_label in devices_raw:
            device.employee_name = emp_name or "Unknown"
            device.api_key_label = key_label or "Legacy/Unknown"
            # Attach branch assignments from new junction table
            branch_assignments = db.query(BindingBranch).filter(
                BindingBranch.binding_id == device.id,
            ).all()
            device.branch_list = []
            for ba in branch_assignments:
                branch = db.query(Branch).filter(Branch.id == ba.branch_id).first()
                if branch:
                    device.branch_list.append({"id": branch.id, "name": branch.name, "binding_branch_id": ba.id})
            # Fallback to legacy branch_id if no junction entries
            if not device.branch_list and device.branch_id:
                branch = db.query(Branch).filter(Branch.id == device.branch_id).first()
                if branch:
                    device.branch_list.append({"id": branch.id, "name": branch.name, "binding_branch_id": None})
            devices.append(device)

        server_url, sn, device_name = get_adms_config()
        db_target = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
        timezone_offset = db_target.timezone_offset if db_target else 7

        # Branches
        branches = db.query(Branch).all()

        # App config
        max_devices_cfg = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
        max_devices = int(max_devices_cfg.value) if max_devices_cfg else 5

        # Today's punch stats
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        today_in = db.query(PunchLog).filter(
            PunchLog.timestamp >= today_start,
            PunchLog.punch_type.ilike('%in%')
        ).count()
        today_out = db.query(PunchLog).filter(
            PunchLog.timestamp >= today_start,
            PunchLog.punch_type.ilike('%out%')
        ).count()

        # Device count per employee
        employee_device_counts = {}
        for d in devices:
            if d.employee_id not in employee_device_counts:
                count = db.query(DeviceBinding).filter(
                    DeviceBinding.employee_id == d.employee_id,
                    DeviceBinding.is_active == True,
                ).count()
                employee_device_counts[d.employee_id] = count if d.employee_id else 0
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
            }
        )
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error in Dashboard:</h3><pre>{traceback.format_exc()}</pre>", status_code=500)

class ADMSConfigRequest(BaseModel):
    server_url: str
    serial_number: str
    device_name: str
    timezone_offset: int

@app.post("/ui/settings")
async def update_settings(config: ADMSConfigRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    target = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
    if not target:
        target = ADMSTarget()
        db.add(target)

    target.server_url = config.server_url
    target.serial_number = config.serial_number
    target.device_name = config.device_name
    target.timezone_offset = config.timezone_offset
    db.commit()
    logger.info(f"ADMS Config updated: {config.server_url} SN={config.serial_number} Alias={config.device_name} (TZ={config.timezone_offset})")
    return {"status": "success"}


# ─── App Config Settings ────────────────────────────────────────────────────

class AppConfigRequest(BaseModel):
    max_devices_per_employee: int = 5

@app.get("/ui/app-settings")
async def get_app_settings(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Return current app config values."""
    max_devices = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    return {
        "max_devices_per_employee": int(max_devices.value) if max_devices else 5,
    }

@app.post("/ui/app-settings")
async def update_app_settings(config: AppConfigRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Update app config values."""
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
    logger.info(f"App config updated: max_devices_per_employee={config.max_devices_per_employee}")
    return {"status": "success"}

class ProfileUpdateRequest(BaseModel):
    username: str
    new_password: str

@app.post("/ui/profile")
async def update_admin_profile(
    req: ProfileUpdateRequest, 
    db: Session = Depends(get_db), 
    admin: AdminUser = Depends(get_current_admin)
):
    if req.username != admin.username:
        raise HTTPException(status_code=403, detail="Cannot change another user's profile")
    
    logger.info(f"Admin '{admin.username}' is updating password...")
    admin.hashed_password = get_password_hash(req.new_password)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    logger.info(f"Admin '{admin.username}' updated their password successfully.")
    return {"status": "success"}

# ─── User Management Routes ──────────────────────────────────────────────────

@app.get("/ui/users")
async def list_users(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    users = db.query(AdminUser).all()
    return [{"id": u.id, "username": u.username, "created_at": u.created_at} for u in users]

class CreateUserRequest(BaseModel):
    username: str
    password: str

@app.post("/ui/users")
async def create_user(req: CreateUserRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    existing = db.query(AdminUser).filter(AdminUser.username == req.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")
    
    new_user = AdminUser(
        username=req.username,
        hashed_password=get_password_hash(req.password)
    )
    db.add(new_user)
    db.commit()
    logger.info(f"Admin '{admin.username}' created new user '{req.username}'")
    return {"status": "success"}

@app.put("/ui/users/{user_id}")
async def update_user(user_id: int, req: CreateUserRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    target_user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
        
    # Logic to update password (and optionally username if it's not the root admin)
    if req.username != target_user.username:
        if target_user.username == "admin":
             raise HTTPException(status_code=400, detail="Cannot rename the root admin user")
        # Check if new username is taken
        existing = db.query(AdminUser).filter(AdminUser.username == req.username).first()
        if existing:
            raise HTTPException(status_code=400, detail="Username already exists")
        target_user.username = req.username
        
    if req.password: # Only update password if provided
        target_user.hashed_password = get_password_hash(req.password)
        
    db.add(target_user)
    db.commit()
    logger.info(f"Admin '{admin.username}' updated user '{target_user.username}'")
    return {"status": "success"}

@app.delete("/ui/users/{user_id}")
async def delete_user(user_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    user_to_delete = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user_to_delete:
        raise HTTPException(status_code=404, detail="User not found")
    
    if user_to_delete.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the root admin user")
        
    if user_to_delete.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
        
    db.delete(user_to_delete)
    db.commit()
    logger.info(f"Admin '{admin.username}' deleted user '{user_to_delete.username}'")
    return {"status": "success"}


@app.post("/ui/test-connection")
async def ui_test_connection(config: ADMSConfigRequest, admin: AdminUser = Depends(get_current_admin)):
    success, message = await test_adms_connection(config.server_url, config.serial_number, config.device_name)
    return {"success": success, "message": message}


# ─── Device Approval Workflow ─────────────────────────────────────────────────

class DeviceLabelRequest(BaseModel):
    label: str
    notes: str = ""

@app.post("/ui/devices/{binding_id}/approve")
async def approve_device(binding_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.registration_status = "approved"
    binding.approved_at = datetime.utcnow()
    binding.approved_by = admin.username
    db.commit()
    logger.info("device_approved", admin=admin.username, binding_id=binding_id, employee_id=binding.employee_id)
    await invalidate_cache("device_config:*")
    return {"status": "approved"}

@app.post("/ui/devices/{binding_id}/suspend")
async def suspend_device(binding_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.registration_status = "suspended"
    db.commit()
    logger.info("device_suspended", admin=admin.username, binding_id=binding_id, employee_id=binding.employee_id)
    await invalidate_cache("device_config:*")
    return {"status": "suspended"}

@app.put("/ui/devices/{binding_id}/label")
async def update_device_label(binding_id: int, req: DeviceLabelRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.device_label = req.label
    binding.notes = req.notes
    db.commit()
    return {"status": "updated"}

@app.post("/ui/devices/{binding_id}/set-active")
async def set_active_device(binding_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Set this specific device as the primary active device for its owner."""
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    
    # If it has an owner, deactivate their other devices
    if binding.employee_id:
        db.query(DeviceBinding).filter(
            DeviceBinding.employee_id == binding.employee_id,
            DeviceBinding.id != binding_id
        ).update({"is_active": False})
        
    binding.is_active = True
    db.commit()
    await invalidate_cache("device_config:*")
    return {"status": "updated"}


@app.delete("/ui/devices/{binding_id}/unbind")
async def unbind_device(binding_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if binding:
        # Also clean up branch assignments
        db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).delete()
        db.delete(binding)
        db.commit()
        logger.info("device_deleted", admin=admin.username, binding_id=binding_id)
    await invalidate_cache("device_config:*")
    return {"status": "success"}

@app.post("/ui/devices/{binding_id}/bind-branch")
async def bind_device_to_branch(binding_id: int, req: dict, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
        
    branch_id = req.get("branch_id")
    if branch_id == "" or branch_id is None:
        binding.branch_id = None
        # Also clear from new junction table
        db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).delete()
    else:
        binding.branch_id = int(branch_id)
        # Mirror to new junction table
        existing = db.query(BindingBranch).filter(
            BindingBranch.binding_id == binding.id,
            BindingBranch.branch_id == int(branch_id),
        ).first()
        if not existing:
            db.add(BindingBranch(binding_id=binding.id, branch_id=int(branch_id)))
        
    db.commit()
    logger.info("device_branch_assigned", employee_id=binding.employee_id, branch_id=branch_id, admin=admin.username)
    await invalidate_cache("device_config:*")
    return {"status": "success"}


# ─── Multi-Branch Assignment CRUD (new junction table) ──────────────────────

@app.get("/ui/devices/{binding_id}/branches")
async def get_device_branches(binding_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """List all branches assigned to a device."""
    assignments = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding_id,
    ).join(Branch, BindingBranch.branch_id == Branch.id).all()
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


@app.post("/ui/devices/{binding_id}/branches/{branch_id}")
async def assign_branch_to_device(binding_id: int, branch_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Add a branch to a device's authorized branches."""
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
    logger.info("branch_assigned_to_device", branch=branch.name, binding_id=binding_id, admin=admin.username)
    await invalidate_cache("device_config:*")
    return {"status": "success"}


@app.delete("/ui/devices/{binding_id}/branches/{branch_id}")
async def remove_branch_from_device(binding_id: int, branch_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Remove a branch from a device's authorized branches."""
    assignment = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding_id,
        BindingBranch.branch_id == branch_id,
    ).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Branch not assigned to this device")
    db.delete(assignment)
    db.commit()
    logger.info("branch_removed_from_device", branch_id=branch_id, binding_id=binding_id, admin=admin.username)
    await invalidate_cache("device_config:*")
    return {"status": "success"}


# ─── Branch CRUD ────────────────────────────────────────────────────────────

class BranchRequest(BaseModel):
    name: str
    latitude: float
    longitude: float
    radius_meters: float
    qr_code_enabled: bool = False
    qr_code_data: Optional[str] = None

@app.get("/ui/branches")
async def get_branches(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    branches = db.query(Branch).all()
    return [{
        "id": b.id,
        "name": b.name,
        "latitude": b.latitude,
        "longitude": b.longitude,
        "radius_meters": b.radius_meters,
        "is_active": b.is_active,
        "qr_code_enabled": b.qr_code_enabled,
        "qr_code_data": b.qr_code_data if b.qr_code_enabled else None,
    } for b in branches]

@app.post("/ui/branches")
async def create_branch(req: BranchRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    new_branch = Branch(
        name=req.name,
        latitude=req.latitude,
        longitude=req.longitude,
        radius_meters=req.radius_meters,
        is_active=True,
        qr_code_enabled=req.qr_code_enabled,
        qr_code_data=req.qr_code_data if req.qr_code_enabled else None,
    )
    db.add(new_branch)
    db.commit()
    await invalidate_cache("device_config:*")
    return {"status": "success"}

@app.put("/ui/branches/{branch_id}")
async def update_branch(branch_id: int, req: BranchRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
        
    branch.name = req.name
    branch.latitude = req.latitude
    branch.longitude = req.longitude
    branch.radius_meters = req.radius_meters
    branch.qr_code_enabled = req.qr_code_enabled
    branch.qr_code_data = req.qr_code_data if req.qr_code_enabled else None
    db.commit()
    await invalidate_cache("device_config:*")
    return {"status": "success"}

@app.delete("/ui/branches/{branch_id}")
async def delete_branch(branch_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if branch:
        # Prevent deletion if it's currently bound
        in_use = db.query(DeviceBinding).filter(DeviceBinding.branch_id == branch_id).first()
        if in_use:
            raise HTTPException(status_code=400, detail="Cannot delete branch while it is assigned to devices.")
        db.delete(branch)
        db.commit()
    await invalidate_cache("device_config:*")
    return {"status": "success"}


@app.post("/ui/devices/{binding_id}/assign")
async def assign_device_employee(binding_id: int, employee_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    
    # Check how many active devices this employee already has
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
            detail=f"Maximum devices reached ({existing_count}/{max_devices}) for this employee."
        )

    binding.employee_id = employee_id
    binding.is_active = True  # All assigned devices start active
    db.commit()
    return {"status": "success"}


# ─── API Key Management UI Routes ────────────────────────────────────────────

@app.post("/ui/api-keys")
async def create_api_key(
    label: str = "Mobile Client",
    expires_in_days: Optional[int] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Generate a new API key for a mobile device.
    Optionally set expiry via expires_in_days query param.
    """
    expires_at = None
    if expires_in_days is not None and expires_in_days > 0:
        expires_at = datetime.utcnow() + timedelta(days=expires_in_days)

    new_key = ApiKey(
        key_value=generate_api_key(),
        label=label,
        expires_at=expires_at,
    )
    db.add(new_key)
    db.commit()
    db.refresh(new_key)
    logger.info("api_key_created", label=label, key_id=new_key.id, admin=admin.username)
    return {
        "key": new_key.key_value,
        "label": new_key.label,
        "id": new_key.id,
        "expires_at": new_key.expires_at.isoformat() if new_key.expires_at else None,
    }


@app.get("/ui/api-keys")
async def list_api_keys(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """List all API keys with usage statistics (values truncated for security)."""
    keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    result = []
    for k in keys:
        device_count = db.query(DeviceBinding).filter(
            DeviceBinding.api_key_id == k.id,
        ).count()

        # Compute expiry info
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


@app.put("/ui/api-keys/{key_id}")
async def rename_api_key(key_id: int, label: str = "", db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Rename an API key's label."""
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    if label.strip():
        key.label = label.strip()
        db.commit()
    return {"status": "success", "label": key.label}


@app.delete("/ui/api-keys/{key_id}")
async def revoke_api_key(key_id: int, hard: bool = False, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Revoke (deactivate) an API key, or permanently delete if already revoked and hard=true."""
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    if hard:
        if key.is_active:
            raise HTTPException(status_code=400, detail="Cannot permanently delete an active key. Revoke it first.")
        # Check if any devices still reference this key
        bindings = db.query(DeviceBinding).filter(DeviceBinding.api_key_id == key_id).all()
        if bindings:
            raise HTTPException(status_code=400, detail=f"Cannot delete: {len(bindings)} device(s) still bound to this key. Soft-revoke instead.")
        db.delete(key)
        db.commit()
        logger.info("api_key_deleted", key_id=key_id, admin=admin.username)
        await invalidate_cache("device_config:*")
        return {"status": "deleted"}
    key.is_active = False
    db.commit()
    logger.info("api_key_revoked", key_id=key_id, admin=admin.username)
    await invalidate_cache("device_config:*")
    return {"status": "revoked"}


@app.post("/ui/api-keys/{key_id}/rotate")
async def rotate_api_key(
    key_id: int,
    grace_period_days: int = 7,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Rotate an API key: generate a new key and expire the old one after a grace period.
    
    The old key remains valid for `grace_period_days` (default 7) so mobile apps
    can transition without disruption. The new key is returned immediately.
    """
    old_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not old_key:
        raise HTTPException(status_code=404, detail="API key not found")
    if not old_key.is_active:
        raise HTTPException(status_code=400, detail="Cannot rotate a revoked or expired key.")

    # Set old key to expire after grace period (or keep existing expiry if sooner)
    grace_end = datetime.utcnow() + timedelta(days=grace_period_days)
    if old_key.expires_at and old_key.expires_at < grace_end:
        # Existing expiry is sooner — keep it
        pass
    else:
        old_key.expires_at = grace_end

    # Create new key with same label
    new_key = ApiKey(
        key_value=generate_api_key(),
        label=old_key.label,
    )
    db.add(new_key)
    db.commit()
    db.refresh(new_key)

    logger.info(
        f"Admin '{admin.username}' rotated API key '{old_key.label}' (id={key_id}). "
        f"Old key expires at {old_key.expires_at}. New key id={new_key.id}."
    )

    return {
        "status": "rotated",
        "old_key_id": old_key.id,
        "old_key_label": old_key.label,
        "old_key_expires_at": old_key.expires_at.isoformat() if old_key.expires_at else None,
        "new_key": new_key.key_value,
        "new_key_id": new_key.id,
        "new_key_label": new_key.label,
    }


class PunchTypePayload(BaseModel):
    code: str
    label: str
    adms_status_code: str
    icon: str = "circle"
    color_hex: str = "#000000"
    display_order: int = 0
    requires_geofence: bool = True
    is_active: bool = True

@app.get("/ui/punch-types", response_model=list[PunchTypeResponse])
async def get_ui_punch_types(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    types = db.query(PunchType).order_by(PunchType.display_order).all()
    return [
        PunchTypeResponse(
            code=t.code, label=t.label, adms_status_code=t.adms_status_code,
            display_order=t.display_order, icon=t.icon, color_hex=t.color_hex,
            requires_geofence=t.requires_geofence,
        ) for t in types
    ]

@app.post("/ui/punch-types")
async def create_punch_type(payload: PunchTypePayload, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    existing = db.query(PunchType).filter(PunchType.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Code already exists")
    pt = PunchType(**payload.dict())
    db.add(pt)
    db.commit()
    await invalidate_cache("punch_types:*")
    return {"status": "created"}

@app.put("/ui/punch-types/{code}")
async def update_punch_type(code: str, payload: PunchTypePayload, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    pt = db.query(PunchType).filter(PunchType.code == code).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Punch type not found")
    for key, value in payload.dict().items():
        setattr(pt, key, value)
    db.commit()
    await invalidate_cache("punch_types:*")
    return {"status": "updated"}

@app.delete("/ui/punch-types/{code}")
async def delete_punch_type(code: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    pt = db.query(PunchType).filter(PunchType.code == code).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Punch type not found")
    db.delete(pt)
    db.commit()
    await invalidate_cache("punch_types:*")
    return {"status": "deleted"}


# ─── ADMS Sync Configuration (UI) ──────────────────────────────────────────

@app.get("/ui/adms-credentials", response_model=ADMSCredentialPayload)
async def get_adms_credentials(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    creds = db.query(ADMSCredential).filter(ADMSCredential.is_active == True).first()
    if not creds:
        return ADMSCredentialPayload(url="", username="", password="")
    return ADMSCredentialPayload(url=creds.url, username=creds.username, password=creds.password)

@app.post("/ui/adms-credentials")
async def save_adms_credentials(payload: ADMSCredentialPayload, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
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

@app.post("/ui/adms-sync")
async def trigger_adms_sync(background_tasks: BackgroundTasks, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    # If ARQ pool is available, enqueue a retry of failed punches
    if arq_pool:
        try:
            job = await arq_pool.enqueue_job("retry_failed_punches")
            logger.info("adms_retry_triggered", job_id=job.job_id, admin=admin.username)
            return {"status": "triggered", "job_id": job.job_id}
        except Exception as e:
            logger.warning("adms_retry_enqueue_failed", error=str(e))
    
    # Fallback: run employee sync directly
    success, msg = sync_employees_from_adms(db)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}

@app.get("/ui/employees/count")
async def get_employee_count(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    count = db.query(Employee).count()
    return {"count": count}


@app.get("/ui/employees/list")
async def list_employees(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    emps = db.query(Employee).order_by(Employee.full_name).all()
    return [{"id": e.employee_id, "name": e.full_name, "dept": e.department} for e in emps]


@app.get("/ui/adms-sync-info")
async def get_adms_sync_info(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Fetch the latest sync status and stats."""
    def get_val(key):
        cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
        return cfg.value if cfg else "Never"
    
    return {
        "last_sync": get_val("last_adms_sync_time"),
        "last_count": get_val("last_adms_sync_count"),
        "last_status": get_val("last_adms_sync_status"),
        "total_employees": db.query(Employee).count()
    }


@app.get("/ui/adms-sync-status")
async def get_adms_sync_status(request: Request, db: Session = Depends(get_db), current_user: AdminUser = Depends(get_current_admin)):
    """Get ADMS sync statistics for the dashboard."""
    # Total records
    total = db.query(PunchLog).count()
    
    # Sync status breakdown (using server_sync_status)
    synced = db.query(PunchLog).filter(PunchLog.server_sync_status == "synced").count()
    pending = db.query(PunchLog).filter(PunchLog.server_sync_status == "pending").count()
    failed = db.query(PunchLog).filter(PunchLog.server_sync_status == "failed").count()
    
    # Recent failures (last 50)
    recent_failures = db.query(PunchLog).filter(
        PunchLog.server_sync_status == "failed"
    ).order_by(PunchLog.timestamp.desc()).limit(50).all()
    
    # Sync activity in last 24 hours
    last_24h = datetime.utcnow() - timedelta(hours=24)
    synced_24h = db.query(PunchLog).filter(
        PunchLog.server_sync_status == "synced",
        PunchLog.synced_at >= last_24h
    ).count()
    
    # ADMS connectivity status (stored in AppConfig by the ARQ worker's heartbeat)
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
    
    # Worker queue health
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


# ─── API V1 ROUTES ───────────────────────────────────────────────────────────

@app.get("/api/v1/app-status", response_model=AppStatusResponse)
async def get_app_status(db: Session = Depends(get_db)):
    config = db.query(AppConfig).filter(AppConfig.key == "min_app_version").first()
    min_ver = config.value if config else "1.0.0"
    return AppStatusResponse(status="ok", min_version=min_ver)

@app.get("/api/v1/device-config", response_model=DeviceConfigResponse)
@limiter.limit("30/minute")
async def get_device_config(
    request: Request,
    device_uuid: str,
    employee_id: Optional[str] = None,
    device_label: str = None,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(verify_api_key)
):
    """
    Mobile app calls this on boot. Auto-registers device if missing.
    Returns registration status and branch config(s) when approved.
    Multi-device: checks max_devices_per_employee before allowing registration.
    Multi-branch: returns ALL branches assigned to this device.
    """
    # ── Try cache first (only for fully approved active configs) ────────────
    # Use api_key.id (int) as part of cache key; device_uuid identifies the binding
    cache_key = f"device_config:{api_key.id}:{device_uuid}"
    cached = await get_cache(cache_key)
    if cached:
        return DeviceConfigResponse(**json.loads(cached))

    # ── Get max-devices config ──────────────────────────────────────────────
    max_cfg = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    max_devices = int(max_cfg.value) if max_cfg else 5

    binding = db.query(DeviceBinding).filter(DeviceBinding.device_uuid == device_uuid).first()

    if not binding:
        # ── New device registration ─────────────────────────────────────────
        if employee_id:
            # Check how many active devices this employee already has
            existing_count = db.query(DeviceBinding).filter(
                DeviceBinding.employee_id == employee_id,
                DeviceBinding.is_active == True,
                DeviceBinding.registration_status.in_(["approved", "active"]),
            ).count()

            if existing_count >= max_devices:
                return DeviceConfigResponse(
                    status="max_devices_reached",
                    message=f"Maximum devices reached ({existing_count}/{max_devices}). "
                            f"Please contact admin to remove an old device.",
                    device_count=existing_count,
                    max_devices=max_devices,
                )

        binding = DeviceBinding(
            employee_id=employee_id,
            device_uuid=device_uuid,
            device_label=device_label,
            registration_status="pending_approval",
            is_active=True,  # All new devices start active
            api_key_id=api_key.id,
        )
        db.add(binding)
        db.commit()
        db.refresh(binding)
        logger.info("device_registered", device_uuid=device_uuid[:8], employee_id=employee_id)

    # Update label or API key association if missing
    needs_commit = False
    if device_label and not binding.device_label:
        binding.device_label = device_label
        needs_commit = True
    if not binding.api_key_id:
        binding.api_key_id = api_key.id
        needs_commit = True
    if needs_commit:
        db.commit()

    # ── Device count for this employee ───────────────────────────────────────
    device_count = 0
    if binding.employee_id:
        device_count = db.query(DeviceBinding).filter(
            DeviceBinding.employee_id == binding.employee_id,
            DeviceBinding.is_active == True,
            DeviceBinding.registration_status.in_(["approved", "active"]),
        ).count()

    # ── Status checks ───────────────────────────────────────────────────────
    status = binding.registration_status
    if status == "pending_approval":
        return DeviceConfigResponse(
            status="pending_approval",
            message="Your device is pending admin approval. Please contact your HR Administrator.",
            device_count=device_count,
            max_devices=max_devices,
        )
    if status == "suspended":
        raise HTTPException(status_code=403, detail="Device suspended. Please contact your HR Administrator.")
    if not binding.is_active:
        raise HTTPException(status_code=403, detail="This device has been deactivated. Contact admin.")

    # ── Branch assignments ──────────────────────────────────────────────────
    branch_assignments = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding.id,
    ).all()

    if not branch_assignments:
        return DeviceConfigResponse(
            status="pending_branch",
            message="Device approved. Waiting for branch assignment by admin.",
            device_count=device_count,
            max_devices=max_devices,
        )

    # Collect all active branches
    branches = []
    for ba in branch_assignments:
        branch = db.query(Branch).filter(
            Branch.id == ba.branch_id,
            Branch.is_active == True,
        ).first()
        if branch:
            branches.append(BranchInfo(
                id=branch.id,
                name=branch.name,
                latitude=branch.latitude,
                longitude=branch.longitude,
                radius_meters=branch.radius_meters,
                qr_code_enabled=branch.qr_code_enabled,
                qr_code_data=branch.qr_code_data if branch.qr_code_enabled else None,
            ))

    if not branches:
        return DeviceConfigResponse(
            status="pending_branch",
            message="All assigned branches are inactive.",
            device_count=device_count,
            max_devices=max_devices,
        )

    response = DeviceConfigResponse(
        status="active",
        branches=branches,
        device_count=device_count,
        max_devices=max_devices,
    )
    # Cache the successful config for 5 minutes
    await set_cache(cache_key, response.model_dump_json(), ttl=300)
    return response


@app.post("/api/v1/punch", response_model=PunchResponse)
@limiter.limit("10/minute")
async def create_punch(
    request: Request,
    punch_req: PunchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(verify_api_key),   # Enforce API key auth
):
    # ── a) Time validation ─────────────────────────────────────────────────────
    try:
        device_time_str = punch_req.timestamp.replace("Z", "")
        if "." in device_time_str:
            device_time_str = device_time_str.split(".")[0]
        device_local_time = datetime.fromisoformat(device_time_str)
        tz_offset = timedelta(minutes=punch_req.tz_offset_minutes)
        server_timestamp = device_local_time - tz_offset
        time_diff = abs((datetime.utcnow() - server_timestamp).total_seconds())
        if time_diff > 300:  # 5 minutes
            raise HTTPException(
                status_code=422,
                detail="Timestamp deviation too large. Please sync your device time."
            )
    except HTTPException:
        raise
    except Exception:
        logger.warning("Failed to parse device timestamp for time validation.")
        server_timestamp = datetime.utcnow()

    # 1. Idempotency check — if we already have this client_punch_id, return the existing record
    if punch_req.client_punch_id:
        existing = db.query(PunchLog).filter(
            PunchLog.client_punch_id == punch_req.client_punch_id
        ).first()
        if existing:
            return {
                "status": "success",
                "message": f"Punch already recorded (duplicate): {existing.punch_type}",
                "server_time": existing.timestamp,
                "log_id": existing.id,
            }

    # 2. Basic validation — allow mock location but tag it (client-reported)
    if not punch_req.biometric_verified:
        raise HTTPException(status_code=400, detail="Biometric verification failed.")

    # 2b. Validate punch_type against registry
    valid_type = db.query(PunchType).filter(
        PunchType.code == punch_req.punch_type,
        PunchType.is_active == True
    ).first()
    if not valid_type:
        raise HTTPException(status_code=400, detail=f"Invalid punch type: '{punch_req.punch_type}'")

    # 3. Device binding check (Enforce server-side identity)
    binding = db.query(DeviceBinding).filter(
        DeviceBinding.device_uuid == punch_req.device_uuid
    ).first()
    
    if not binding:
        raise HTTPException(status_code=403, detail="Device not registered. Please register in the app settings first.")
    if not binding.employee_id:
        raise HTTPException(status_code=403, detail="Device not yet assigned to an employee by administrator.")
    
    # Securely use the server-side bound employee_id
    effective_employee_id = binding.employee_id
    
    if binding.registration_status == "pending_approval":
        raise HTTPException(status_code=403, detail="Device pending admin approval.")
    if binding.registration_status == "suspended":
        raise HTTPException(status_code=403, detail="Device suspended. Contact admin.")
    if not binding.is_active:
        raise HTTPException(status_code=403, detail="Device has been deactivated. Contact admin.")

    # 3b. Get all assigned branches for this device
    branch_assignments = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding.id,
    ).all()
    if not branch_assignments:
        raise HTTPException(status_code=403, detail="Device not assigned to any branch. Please contact Admin.")

    assigned_branches = []
    for ba in branch_assignments:
        branch = db.query(Branch).filter(
            Branch.id == ba.branch_id,
            Branch.is_active == True,
        ).first()
        if branch:
            assigned_branches.append(branch)

    if not assigned_branches:
        raise HTTPException(status_code=403, detail="All assigned branches are inactive. Please contact Admin.")

    # 3c. Multi-branch geofencing check
    in_fence, distance, best_branch = is_within_any_fence(
        punch_req.latitude, punch_req.longitude, assigned_branches
    )
    if not in_fence:
        raise HTTPException(
            status_code=400,
            detail=f"Outside assigned branches. Nearest: {best_branch} ({distance:.0f}m away)."
        )

    # ── d) Duplicate punch detection ────────────────────────────────────────────
    recent_cutoff = datetime.utcnow() - timedelta(minutes=5)
    recent_punch = db.query(PunchLog).filter(
        and_(
            PunchLog.employee_id == effective_employee_id,
            PunchLog.punch_type == punch_req.punch_type,
            PunchLog.timestamp >= recent_cutoff
        )
    ).first()
    if recent_punch:
        elapsed = int((datetime.utcnow() - recent_punch.timestamp).total_seconds() / 60)
        raise HTTPException(
            status_code=409,
            detail=f"Duplicate {punch_req.punch_type} detected within 5 minutes"
        )

    # ── b) Rate limit per employee per day ──────────────────────────────────────
    MAX_DAILY_PUNCHES = settings.max_daily_punches
    daily_count = db.query(PunchLog).filter(
        PunchLog.employee_id == effective_employee_id,
        func.date(PunchLog.timestamp) == func.current_date()
    ).count()
    if daily_count >= MAX_DAILY_PUNCHES:
        raise HTTPException(
            status_code=400,
            detail="Maximum daily punches exceeded"
        )

    # ── c) Client-reported flags tagging ───────────────────────────────────────
    notes_parts = []
    if punch_req.is_mock_location:
        notes_parts.append("mock_location: client-reported")
    if not punch_req.gps_time_validated:
        notes_parts.append("gps_time_validated: client-reported")
    notes = "; ".join(notes_parts) if notes_parts else None

    # 5. Record punch
    try:
        device_time_str = punch_req.timestamp.replace("Z", "")
        if "." in device_time_str:
            device_time_str = device_time_str.split(".")[0]
        device_local_time = datetime.fromisoformat(device_time_str)
        server_time_utc = device_local_time - tz_offset
        
        logger.info("punch_timestamp_parsed", device_local_time=str(device_local_time), gps_validated=punch_req.gps_time_validated)
    except Exception as e:
        logger.warning("punch_timestamp_parse_failed", error=str(e))
        server_time_utc = datetime.utcnow()

    log = PunchLog(
        employee_id=effective_employee_id,
        device_uuid=punch_req.device_uuid,
        timestamp=server_time_utc,
        latitude=punch_req.latitude,
        longitude=punch_req.longitude,
        is_mock_location=punch_req.is_mock_location,
        biometric_verified=punch_req.biometric_verified,
        punch_type=punch_req.punch_type,
        tz_offset_minutes=punch_req.tz_offset_minutes,
        adms_status="pending",
        client_punch_id=punch_req.client_punch_id,
        gps_time_validated=punch_req.gps_time_validated,
        notes=notes,
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    # 6. Enqueue ADMS sync via ARQ worker (replaces direct background push)
    if arq_pool:
        try:
            await arq_pool.enqueue_job("sync_punches_to_adms", log.id)
        except Exception as e:
            logger.warning("arq_enqueue_failed", error=str(e))

    logger.info("punch_submitted",
                employee_id=effective_employee_id,
                punch_type=punch_req.punch_type,
                log_id=log.id,
                geofence_valid=in_fence)
    return {
        "status": "success",
        "message": f"Punch recorded: {punch_req.punch_type}",
        "server_time": server_time_utc,
        "log_id": log.id,
    }


# ─── Batch Punch Endpoint ────────────────────────────────────────────────────

@app.post("/api/v1/punch/batch", response_model=BatchPunchResponse)
@limiter.limit("10/minute")
async def create_batch_punch(
    request: Request,
    batch_req: BatchPunchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(verify_api_key),
):
    """Accept up to 50 offline punches in one request for efficient sync."""
    if len(batch_req.punches) > 50:
        raise HTTPException(status_code=400, detail="Batch size limit is 50 punches.")

    results = []
    synced = 0
    failed = 0

    for punch in batch_req.punches:
        try:
            # ── a) Time validation ─────────────────────────────────────────
            try:
                device_time_str = punch.timestamp.replace("Z", "")
                if "." in device_time_str:
                    device_time_str = device_time_str.split(".")[0]
                device_local_time = datetime.fromisoformat(device_time_str)
                server_time_utc = device_local_time - timedelta(minutes=punch.tz_offset_minutes)
                time_diff = abs((datetime.utcnow() - server_time_utc).total_seconds())
                if time_diff > 300:
                    results.append(BatchPunchResult(
                        client_punch_id=punch.client_punch_id,
                        status="error",
                        error="Timestamp deviation too large. Please sync your device time.",
                    ))
                    failed += 1
                    continue
            except Exception:
                server_time_utc = datetime.utcnow()

            # Idempotency check
            if punch.client_punch_id:
                existing = db.query(PunchLog).filter(
                    PunchLog.client_punch_id == punch.client_punch_id
                ).first()
                if existing:
                    results.append(BatchPunchResult(
                        client_punch_id=punch.client_punch_id,
                        status="duplicate",
                        log_id=existing.id,
                    ))
                    synced += 1
                    continue

            # Basic validation — allow mock location but tag it
            if not punch.biometric_verified:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error="Biometric verification failed.",
                ))
                failed += 1
                continue

            # Validate punch_type against registry
            valid_type = db.query(PunchType).filter(
                PunchType.code == punch.punch_type,
                PunchType.is_active == True
            ).first()
            if not valid_type:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error=f"Invalid punch type: '{punch.punch_type}'",
                ))
                failed += 1
                continue

            # Validate device (Enforce server-side identity)
            binding = db.query(DeviceBinding).filter(
                DeviceBinding.device_uuid == punch.device_uuid,
            ).first()
            
            if not binding or not binding.employee_id:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error="Device not authorized or unassigned",
                ))
                failed += 1
                continue
                
            if binding.registration_status not in ("approved", "active") or not binding.is_active:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error="Device not active",
                ))
                failed += 1
                continue

            # Securely use the server-side bound employee_id
            effective_employee_id = binding.employee_id

            # Multi-branch: get all assigned branches
            branch_assignments = db.query(BindingBranch).filter(
                BindingBranch.binding_id == binding.id,
            ).all()
            if not branch_assignments:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error="No branch assigned",
                ))
                failed += 1
                continue

            assigned_branches = []
            for ba in branch_assignments:
                branch = db.query(Branch).filter(
                    Branch.id == ba.branch_id,
                    Branch.is_active == True,
                ).first()
                if branch:
                    assigned_branches.append(branch)

            if not assigned_branches:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error="All assigned branches inactive",
                ))
                failed += 1
                continue

            in_fence, distance, best_branch = is_within_any_fence(
                punch.latitude, punch.longitude, assigned_branches
            )
            if not in_fence:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error=f"Outside assigned branches. Nearest: {best_branch} ({distance:.0f}m away)",
                ))
                failed += 1
                continue

            # ── d) Duplicate punch detection ────────────────────────────────
            recent_cutoff = datetime.utcnow() - timedelta(minutes=5)
            recent_punch = db.query(PunchLog).filter(
                and_(
                    PunchLog.employee_id == effective_employee_id,
                    PunchLog.punch_type == punch.punch_type,
                    PunchLog.timestamp >= recent_cutoff
                )
            ).first()
            if recent_punch:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error=f"Duplicate {punch.punch_type} detected within 5 minutes",
                ))
                failed += 1
                continue

            # ── b) Rate limit per employee per day ──────────────────────────
            MAX_DAILY_PUNCHES = settings.max_daily_punches
            daily_count = db.query(PunchLog).filter(
                PunchLog.employee_id == effective_employee_id,
                func.date(PunchLog.timestamp) == func.current_date()
            ).count()
            if daily_count >= MAX_DAILY_PUNCHES:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error="Maximum daily punches exceeded",
                ))
                failed += 1
                continue

            # ── c) Client-reported flags tagging ───────────────────────────
            notes_parts = []
            if punch.is_mock_location:
                notes_parts.append("mock_location: client-reported")
            if not punch.gps_time_validated:
                notes_parts.append("gps_time_validated: client-reported")
            notes = "; ".join(notes_parts) if notes_parts else None

            # Parse timestamp
            try:
                device_time_str = punch.timestamp.replace("Z", "")
                if "." in device_time_str:
                    device_time_str = device_time_str.split(".")[0]
                device_local_time = datetime.fromisoformat(device_time_str)
                server_time_utc = device_local_time - timedelta(minutes=punch.tz_offset_minutes)
            except Exception:
                server_time_utc = datetime.utcnow()

            log = PunchLog(
                employee_id=effective_employee_id,
                device_uuid=punch.device_uuid,
                timestamp=server_time_utc,
                latitude=punch.latitude,
                longitude=punch.longitude,
                is_mock_location=punch.is_mock_location,
                biometric_verified=punch.biometric_verified,
                punch_type=punch.punch_type,
                tz_offset_minutes=punch.tz_offset_minutes,
                adms_status="pending",
                client_punch_id=punch.client_punch_id,
                gps_time_validated=punch.gps_time_validated,
                notes=notes,
            )
            db.add(log)
            db.commit()
            db.refresh(log)
            # Enqueue ADMS sync via ARQ worker
            if arq_pool:
                try:
                    await arq_pool.enqueue_job("sync_punches_to_adms", log.id)
                except Exception as e:
                    logger.warning("arq_enqueue_failed", error=str(e))

            results.append(BatchPunchResult(
                client_punch_id=punch.client_punch_id,
                status="success",
                log_id=log.id,
            ))
            synced += 1

        except HTTPException as e:
            results.append(BatchPunchResult(
                client_punch_id=punch.client_punch_id,
                status="error",
                error=e.detail,
            ))
            failed += 1
        except Exception as e:
            results.append(BatchPunchResult(
                client_punch_id=punch.client_punch_id,
                status="error",
                error=str(e),
            ))
            failed += 1

    return BatchPunchResponse(synced=synced, failed=failed, results=results)


# ─── Punch Types Endpoint ────────────────────────────────────────────────────

@app.get("/api/v1/punch-types", response_model=list[PunchTypeResponse])
@limiter.limit("30/minute")
async def get_punch_types(
    request: Request,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(verify_api_key),
):
    """Mobile app fetches available punch types on startup."""
    cache_key = f"punch_types:{_.id}"
    cached = await get_cache(cache_key)
    if cached:
        return [PunchTypeResponse(**item) for item in json.loads(cached)]

    types = db.query(PunchType).filter(PunchType.is_active == True).order_by(PunchType.display_order).all()
    result = [
        PunchTypeResponse(
            code=t.code, label=t.label, adms_status_code=t.adms_status_code,
            display_order=t.display_order, icon=t.icon, color_hex=t.color_hex,
            requires_geofence=t.requires_geofence,
        ) for t in types
    ]
    # Cache for 10 minutes (punch types rarely change)
    await set_cache(cache_key, json.dumps([r.model_dump() for r in result]), ttl=600)
    return result


# ─── Punch History (Cursor-based Pagination) ─────────────────────────────────

@app.get("/api/v1/punch-history")
async def get_punch_history(
    api_key: ApiKey = Depends(verify_api_key),
    employee_id: Optional[str] = None,
    cursor: Optional[str] = None,  # ISO timestamp for cursor-based pagination
    limit: int = 50,
    db: Session = Depends(get_db)
):
    """Paginated punch log history with cursor-based pagination."""
    query = db.query(PunchLog)

    if employee_id:
        query = query.filter(PunchLog.employee_id == employee_id)

    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor)
            query = query.filter(PunchLog.timestamp < cursor_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor format. Use ISO timestamp.")

    logs = query.order_by(PunchLog.timestamp.desc()).limit(limit + 1).all()

    has_more = len(logs) > limit
    if has_more:
        logs = logs[:limit]

    next_cursor = logs[-1].timestamp.isoformat() if logs and has_more else None

    def serialize_punch_log(log):
        return {
            "id": log.id,
            "employee_id": log.employee_id,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "punch_type": log.punch_type,
            "latitude": log.latitude,
            "longitude": log.longitude,
            "is_mock_location": log.is_mock_location,
            "biometric_verified": log.biometric_verified,
            "adms_status": log.adms_status,
            "tz_offset_minutes": log.tz_offset_minutes,
        }

    return {
        "data": [serialize_punch_log(log) for log in logs],
        "next_cursor": next_cursor,
        "has_more": has_more
    }


# ─── Punch Log Export (Streaming CSV) ────────────────────────────────────────

@app.get("/ui/logs/export")
async def export_punch_logs(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Export punch logs as CSV with server-side streaming for large datasets."""
    query = db.query(PunchLog, Employee.full_name).\
        outerjoin(Employee, PunchLog.employee_id == Employee.employee_id)

    if from_date:
        query = query.filter(PunchLog.timestamp >= datetime.fromisoformat(from_date))
    if to_date:
        query = query.filter(PunchLog.timestamp <= datetime.fromisoformat(to_date))

    query = query.order_by(PunchLog.timestamp).yield_per(100)

    async def generate():
        yield "Employee ID,Timestamp,Punch Type,Latitude,Longitude,Biometric,Mock Location\n"
        for log, name in query:
            yield f"{log.employee_id},{log.timestamp.isoformat() if log.timestamp else ''},{log.punch_type},{log.latitude},{log.longitude},{log.biometric_verified},{log.is_mock_location}\n"

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=attendance_logs.csv"}
    )


# ═══════════════════ SUPERVISOR / MANAGER ENDPOINTS ═══════════════════

@app.get("/api/v1/supervisor/team")
async def get_team_attendance(
    request: Request,
    device_uuid: Optional[str] = None,
    date: Optional[str] = None,
    api_key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """Get attendance status of all team members for a supervisor."""
    # Find the device binding from the API key
    binding = db.query(DeviceBinding).filter(
        DeviceBinding.api_key_id == api_key.id
    ).first()
    if device_uuid:
        binding = db.query(DeviceBinding).filter(
            DeviceBinding.device_uuid == device_uuid
        ).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")

    supervisor_id = binding.employee_id

    # Get team members
    team = db.query(EmployeeSupervisor).filter(
        EmployeeSupervisor.supervisor_id == supervisor_id
    ).all()

    if not team:
        return {"team": []}

    employee_ids = [t.employee_id for t in team]
    employees = db.query(Employee).filter(Employee.employee_id.in_(employee_ids)).all()
    employee_map = {e.employee_id: e.name for e in employees}

    # Get today's punches
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    if date:
        today = datetime.fromisoformat(date).replace(hour=0, minute=0, second=0, microsecond=0)

    tomorrow = today + timedelta(days=1)

    result = []
    for emp_id in employee_ids:
        punches = db.query(PunchLog).filter(
            PunchLog.employee_id == emp_id,
            PunchLog.timestamp >= today,
            PunchLog.timestamp < tomorrow
        ).order_by(PunchLog.timestamp).all()

        first_punch = punches[0] if punches else None
        last_punch = punches[-1] if punches else None

        # Calculate total hours
        total_hours = None
        if len(punches) >= 2:
            total_seconds = (last_punch.timestamp - first_punch.timestamp).total_seconds()
            total_hours = round(total_seconds / 3600, 2)

        result.append({
            "employee_id": emp_id,
            "name": employee_map.get(emp_id, emp_id),
            "today_punched": len(punches) > 0,
            "first_punch_time": first_punch.timestamp.isoformat() if first_punch else None,
            "last_punch_time": last_punch.timestamp.isoformat() if last_punch else None,
            "total_hours_today": total_hours,
            "is_late": first_punch and first_punch.timestamp.hour >= 9,
        })

    return {"team": result, "date": today.date().isoformat()}


@app.get("/api/v1/supervisor/team/{employee_id}/history")
async def get_employee_history(
    employee_id: str,
    days: int = 7,
    device_uuid: Optional[str] = None,
    api_key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """Get detailed punch history for a specific team member."""
    start_date = datetime.utcnow() - timedelta(days=days)

    punches = db.query(PunchLog).filter(
        PunchLog.employee_id == employee_id,
        PunchLog.timestamp >= start_date
    ).order_by(PunchLog.timestamp.desc()).limit(100).all()

    return {
        "employee_id": employee_id,
        "days": days,
        "punches": [
            {
                "id": p.id,
                "timestamp": p.timestamp.isoformat(),
                "punch_type": p.punch_type,
                "latitude": p.latitude,
                "longitude": p.longitude,
                "biometric_verified": p.biometric_verified,
                "is_mock_location": p.is_mock_location,
            }
            for p in punches
        ]
    }


# === CORRECTION ENDPOINTS ===

@app.post("/api/v1/attendance/correction")
async def request_correction(
    request_data: CorrectionRequest,
    api_key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """Submit an attendance correction request."""
    correction = AttendanceCorrection(
        employee_id=request_data.employee_id,
        original_punch_id=request_data.original_punch_id,
        correction_type=request_data.correction_type,
        description=request_data.description,
        proposed_timestamp=datetime.fromisoformat(request_data.proposed_timestamp) if request_data.proposed_timestamp else None,
        proposed_punch_type=request_data.proposed_punch_type,
        status='pending',
    )
    db.add(correction)
    db.commit()
    db.refresh(correction)
    return {"status": "submitted", "correction_id": correction.id}


@app.get("/api/v1/supervisor/corrections")
async def get_pending_corrections(
    device_uuid: Optional[str] = None,
    api_key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """Get pending correction requests for the supervisor's team."""
    # Find the device binding from the API key
    binding = db.query(DeviceBinding).filter(
        DeviceBinding.api_key_id == api_key.id
    ).first()
    if device_uuid:
        binding = db.query(DeviceBinding).filter(
            DeviceBinding.device_uuid == device_uuid
        ).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")

    # Get team employee IDs
    team = db.query(EmployeeSupervisor).filter(
        EmployeeSupervisor.supervisor_id == binding.employee_id
    ).all()
    employee_ids = [t.employee_id for t in team]

    # Get pending corrections
    corrections = db.query(AttendanceCorrection).filter(
        AttendanceCorrection.employee_id.in_(employee_ids),
        AttendanceCorrection.status == 'pending'
    ).order_by(AttendanceCorrection.created_at.desc()).all()

    return {
        "corrections": [
            {
                "id": c.id,
                "employee_id": c.employee_id,
                "correction_type": c.correction_type,
                "description": c.description,
                "proposed_timestamp": c.proposed_timestamp.isoformat() if c.proposed_timestamp else None,
                "proposed_punch_type": c.proposed_punch_type,
                "created_at": c.created_at.isoformat(),
            }
            for c in corrections
        ]
    }


@app.post("/api/v1/supervisor/corrections/{correction_id}/review")
async def review_correction(
    correction_id: int,
    review: CorrectionReview,
    device_uuid: Optional[str] = None,
    api_key: ApiKey = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """Approve or reject an attendance correction request."""
    correction = db.query(AttendanceCorrection).filter(
        AttendanceCorrection.id == correction_id
    ).first()

    if not correction:
        raise HTTPException(status_code=404, detail="Correction not found")

    # Find the supervisor from their API key
    binding = db.query(DeviceBinding).filter(
        DeviceBinding.api_key_id == api_key.id
    ).first()
    if device_uuid:
        binding = db.query(DeviceBinding).filter(
            DeviceBinding.device_uuid == device_uuid
        ).first()

    correction.status = review.status
    correction.reviewed_by = binding.employee_id if binding else "unknown"
    correction.reviewed_at = datetime.utcnow()
    correction.review_notes = review.notes

    # If approved, update the punch log
    if review.status == 'approved' and correction.original_punch_id:
        punch = db.query(PunchLog).filter(PunchLog.id == correction.original_punch_id).first()
        if punch:
            if correction.proposed_punch_type:
                punch.punch_type = correction.proposed_punch_type
            if correction.proposed_timestamp:
                punch.timestamp = correction.proposed_timestamp

    db.commit()
    return {"status": review.status, "correction_id": correction_id}


# ═══════════════════ ADMIN SUPERVISOR MANAGEMENT ═══════════════════

@app.post("/ui/supervisors/assign")
async def assign_supervisor(
    assignment: SupervisorAssignment,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Assign a supervisor to an employee (admin only)."""
    existing = db.query(EmployeeSupervisor).filter(
        EmployeeSupervisor.supervisor_id == assignment.supervisor_id,
        EmployeeSupervisor.employee_id == assignment.employee_id
    ).first()

    if existing:
        raise HTTPException(status_code=409, detail="Assignment already exists")

    mapping = EmployeeSupervisor(
        supervisor_id=assignment.supervisor_id,
        employee_id=assignment.employee_id
    )
    db.add(mapping)
    db.commit()
    return {"status": "assigned"}


@app.delete("/ui/supervisors/assign/{mapping_id}")
async def remove_supervisor(
    mapping_id: int,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Remove a supervisor-employee assignment."""
    mapping = db.query(EmployeeSupervisor).filter(
        EmployeeSupervisor.id == mapping_id
    ).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Assignment not found")

    db.delete(mapping)
    db.commit()
    return {"status": "removed"}


@app.get("/ui/supervisors/list")
async def list_supervisors(
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """List all supervisor assignments."""
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
        ]
    }


@app.get("/ui/supervisors")
async def supervisor_management(
    request: Request,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "section": "supervisors",
            "devices": db.query(DeviceBinding).all(),
            "branches": db.query(Branch).all(),
            "api_keys": db.query(ApiKey).all(),
            "punch_types": db.query(PunchType).all(),
            "adms_targets": db.query(ADMSTarget).all(),
            "app_settings": {"max_devices_per_employee": 5},
        },
    )


@app.get("/ui/help")
async def help_page(
    request: Request,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """Render the help/documentation page."""
    logger.info("rendering_help_page", client_ip=request.client.host)
    return templates.TemplateResponse(
        request=request,
        name="help.html",
        context={
            "request": request,
            "app_settings": {"max_devices_per_employee": 5},
        },
    )


# ─── Health Check & Metrics ───────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Health check endpoint for container orchestration and monitoring."""
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db_ok = True
        db.close()
    except Exception:
        pass

    adms_ok = _handshake_state.get("handshake_done", False)

    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "adms": "connected" if adms_ok else "disconnected",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0"
    }


# ─── Selfie Upload & Serving Endpoints ───────────────────────────────────────

@app.post("/api/v1/punch/selfie")
async def upload_selfie(
    punch_id: int,
    file: UploadFile = File(...),
    api_key: str = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """Upload a selfie image associated with a punch record."""
    # Verify punch exists
    punch = db.query(PunchLog).filter(PunchLog.id == punch_id).first()
    if not punch:
        raise HTTPException(status_code=404, detail="Punch not found")

    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/webp"]
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, and WebP images are allowed")

    # Save file
    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"selfie_{punch_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)

    content = await file.read()

    # Optional: Validate file size (max 5MB)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum 5MB allowed")

    with open(filepath, "wb") as f:
        f.write(content)

    # Update punch log
    punch.selfie_filename = filename
    db.commit()

    return {"status": "success", "filename": filename}


@app.get("/ui/selfie/{filename}")
async def get_selfie(filename: str, current_user: AdminUser = Depends(get_current_admin)):
    """Serve selfie images for admin review."""
    filepath = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Selfie not found")
    return FileResponse(filepath, media_type="image/jpeg")


# ─── FCM Token Registration ──────────────────────────────────────────────────

@app.post("/api/v1/device/fcm-token")
async def update_fcm_token(
    token_data: dict,
    api_key: str = Depends(verify_api_key),
    db: Session = Depends(get_db)
):
    """Register or update FCM token for push notifications."""
    device_uuid = token_data.get("device_uuid")
    fcm_token = token_data.get("fcm_token")

    if not device_uuid or not fcm_token:
        raise HTTPException(status_code=400, detail="device_uuid and fcm_token required")

    binding = db.query(DeviceBinding).filter(
        DeviceBinding.device_uuid == device_uuid
    ).first()

    if binding:
        binding.fcm_token = fcm_token
        db.commit()
        return {"status": "updated"}

    raise HTTPException(status_code=404, detail="Device not found")
