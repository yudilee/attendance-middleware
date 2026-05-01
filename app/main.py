import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import and_
from typing import Optional
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.requests import Request
from pydantic import BaseModel
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import Response

from app.services.auth import verify_api_key, generate_api_key
from app.services.auth_ui import get_password_hash, verify_password, create_access_token, get_current_admin

from app.database.models import init_db, SessionLocal, DeviceBinding, PunchLog, ADMSTarget, Branch, ApiKey, ADMSRegisteredEmployee, AdminUser, PunchType, Employee, AppConfig, ADMSCredential, BindingBranch
from app.api.v1.schemas import (
    PunchRequest, PunchResponse, DeviceConfigResponse,
    BatchPunchRequest, BatchPunchResponse, BatchPunchResult, PunchTypeResponse,
    ADMSCredentialPayload, AppStatusResponse, BranchInfo
)
from app.services.adms_scraper import sync_employees_from_adms
from app.services.geo import is_within_fence, is_within_any_fence
from app.services.adms_service import push_to_adms, adms_heartbeat_loop, retry_failed_pushes, get_adms_config, test_adms_connection, _handshake_state

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def adms_sync_loop():
    """Background task to sync ADMS employees daily at 2:00 AM (or whatever internal interval we set).
       For simplicity, we'll run it once every 24 hours."""
    while True:
        try:
            db = SessionLocal()
            try:
                logger.info("Running scheduled ADMS employee sync...")
                success, msg = sync_employees_from_adms(db)
                if not success:
                    logger.warning(f"ADMS Sync failed: {msg}")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Error in adms_sync_loop: {e}")
        
        await asyncio.sleep(86400) # Sleep 24 hours


# ─── Lifespan (replaces deprecated @app.on_event) ────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    init_db()
    
    # Auto-initialize default admin if none exists
    db = SessionLocal()
    try:
        admin = db.query(AdminUser).first()
        if not admin:
            logger.info("Creating default admin account...")
            default_admin = AdminUser(username="admin", hashed_password=get_password_hash("admin"))
            db.add(default_admin)
            db.commit()
    finally:
        db.close()

    logger.info("Starting ADMS heartbeat loop...")
    heartbeat_task = asyncio.create_task(adms_heartbeat_loop())
    logger.info("Starting ADMS push retry loop...")
    retry_task = asyncio.create_task(retry_failed_pushes())
    logger.info("Starting ADMS employee sync loop...")
    sync_task = asyncio.create_task(adms_sync_loop())
    yield
    # Graceful shutdown
    heartbeat_task.cancel()
    retry_task.cancel()
    sync_task.cancel()
    try:
        await asyncio.gather(heartbeat_task, retry_task, sync_task, return_exceptions=True)
    except Exception:
        pass
    logger.info("ADMS background tasks stopped.")


app = FastAPI(title="Secure Geo-Fenced Attendance Aggregator", lifespan=lifespan)

# Mount Static Files and Templates
static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=template_path)


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

@app.post("/ui/devices/{employee_id}/approve")
async def approve_device(employee_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.registration_status = "approved"
    binding.approved_at = datetime.utcnow()
    binding.approved_by = admin.username
    db.commit()
    logger.info(f"Admin '{admin.username}' approved device for {employee_id}")
    return {"status": "approved"}

@app.post("/ui/devices/{employee_id}/suspend")
async def suspend_device(employee_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.registration_status = "suspended"
    db.commit()
    logger.info(f"Admin '{admin.username}' suspended device for {employee_id}")
    return {"status": "suspended"}

@app.put("/ui/devices/{employee_id}/label")
async def update_device_label(employee_id: str, req: DeviceLabelRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.device_label = req.label
    binding.notes = req.notes
    db.commit()
    return {"status": "updated"}

@app.post("/ui/devices/{employee_id}/set-active")
async def set_active_device(employee_id: str, req: dict, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Toggle which device is active for this employee (multi-device: deactivate others)."""
    device_uuid = req.get("device_uuid")
    bindings = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).all()
    for b in bindings:
        b.is_active = (b.device_uuid == device_uuid)
    db.commit()
    return {"status": "updated"}


@app.delete("/ui/unbind/{employee_id}")
async def unbind_device(employee_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).first()
    if binding:
        db.delete(binding)
        db.commit()
    return {"status": "success"}

@app.post("/ui/devices/{employee_id}/bind-branch")
async def bind_device_to_branch(employee_id: str, req: dict, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).first()
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
    logger.info(f"Assigned device {employee_id} to branch {branch_id}")
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
    logger.info(f"Assigned branch {branch.name} to device binding {binding_id}")
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
    logger.info(f"Removed branch {branch_id} from device binding {binding_id}")
    return {"status": "success"}


# ─── Branch CRUD ────────────────────────────────────────────────────────────

class BranchRequest(BaseModel):
    name: str
    latitude: float
    longitude: float
    radius_meters: float

@app.get("/ui/branches")
async def get_branches(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    branches = db.query(Branch).all()
    return [{
        "id": b.id,
        "name": b.name,
        "latitude": b.latitude,
        "longitude": b.longitude,
        "radius_meters": b.radius_meters,
        "is_active": b.is_active
    } for b in branches]

@app.post("/ui/branches")
async def create_branch(req: BranchRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    new_branch = Branch(
        name=req.name,
        latitude=req.latitude,
        longitude=req.longitude,
        radius_meters=req.radius_meters,
        is_active=True
    )
    db.add(new_branch)
    db.commit()
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
    db.commit()
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
    return {"status": "success"}


@app.post("/ui/devices/{binding_id}/assign")
async def assign_device_employee(binding_id: int, employee_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    
    binding.employee_id = employee_id
    binding.is_active = True  # All assigned devices start active
    db.commit()
    return {"status": "success"}


# ─── API Key Management UI Routes ────────────────────────────────────────────

@app.post("/ui/api-keys")
async def create_api_key(label: str = "Mobile Client", db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Generate a new API key for a mobile device."""
    new_key = ApiKey(key_value=generate_api_key(), label=label)
    db.add(new_key)
    db.commit()
    db.refresh(new_key)
    return {"key": new_key.key_value, "label": new_key.label, "id": new_key.id}


@app.get("/ui/api-keys")
async def list_api_keys(db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """List all API keys with usage statistics (values truncated for security)."""
    keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    result = []
    for k in keys:
        device_count = db.query(DeviceBinding).filter(
            DeviceBinding.api_key_id == k.id,
        ).count()

        result.append({
            "id": k.id,
            "label": k.label,
            "key_preview": k.key_value[:12] + "...",
            "is_active": k.is_active,
            "created_at": k.created_at.isoformat() if k.created_at else None,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
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
        return {"status": "deleted"}
    key.is_active = False
    db.commit()
    return {"status": "revoked"}


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
    return {"status": "created"}

@app.put("/ui/punch-types/{code}")
async def update_punch_type(code: str, payload: PunchTypePayload, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    pt = db.query(PunchType).filter(PunchType.code == code).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Punch type not found")
    for key, value in payload.dict().items():
        setattr(pt, key, value)
    db.commit()
    return {"status": "updated"}

@app.delete("/ui/punch-types/{code}")
async def delete_punch_type(code: str, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    pt = db.query(PunchType).filter(PunchType.code == code).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Punch type not found")
    db.delete(pt)
    db.commit()
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


# ─── API V1 ROUTES ───────────────────────────────────────────────────────────

@app.get("/api/v1/app-status", response_model=AppStatusResponse)
async def get_app_status(db: Session = Depends(get_db)):
    config = db.query(AppConfig).filter(AppConfig.key == "min_app_version").first()
    min_ver = config.value if config else "1.0.0"
    return AppStatusResponse(status="ok", min_version=min_ver)

@app.get("/api/v1/device-config", response_model=DeviceConfigResponse)
async def get_device_config(
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
        logger.info(f"New device registered (UUID: {device_uuid[:8]}...) for employee {employee_id}")

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
            ))

    if not branches:
        return DeviceConfigResponse(
            status="pending_branch",
            message="All assigned branches are inactive.",
            device_count=device_count,
            max_devices=max_devices,
        )

    return DeviceConfigResponse(
        status="active",
        branches=branches,
        device_count=device_count,
        max_devices=max_devices,
    )


@app.post("/api/v1/punch", response_model=PunchResponse)
async def create_punch(
    request: PunchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(verify_api_key),   # Enforce API key auth
):
    # 1. Idempotency check — if we already have this client_punch_id, return the existing record
    if request.client_punch_id:
        existing = db.query(PunchLog).filter(
            PunchLog.client_punch_id == request.client_punch_id
        ).first()
        if existing:
            return {
                "status": "success",
                "message": f"Punch already recorded (duplicate): {existing.punch_type}",
                "server_time": existing.timestamp,
                "log_id": existing.id,
            }

    # 2. Basic validation
    if request.is_mock_location:
        raise HTTPException(status_code=400, detail="Mock location detected. Punch rejected.")
    if not request.biometric_verified:
        raise HTTPException(status_code=400, detail="Biometric verification failed.")

    # 2b. Validate punch_type against registry
    valid_type = db.query(PunchType).filter(
        PunchType.code == request.punch_type,
        PunchType.is_active == True
    ).first()
    if not valid_type:
        raise HTTPException(status_code=400, detail=f"Invalid punch type: '{request.punch_type}'")

    # 3. Device binding check (Enforce server-side identity)
    binding = db.query(DeviceBinding).filter(
        DeviceBinding.device_uuid == request.device_uuid
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
        request.latitude, request.longitude, assigned_branches
    )
    if not in_fence:
        raise HTTPException(
            status_code=400,
            detail=f"Outside assigned branches. Nearest: {best_branch} ({distance:.0f}m away)."
        )

    # 4. Duplicate punch prevention (block same punch_type within 5 minutes)
    recent_cutoff = datetime.utcnow() - timedelta(minutes=5)
    recent_punch = db.query(PunchLog).filter(
        and_(
            PunchLog.employee_id == effective_employee_id,
            PunchLog.punch_type == request.punch_type,
            PunchLog.timestamp >= recent_cutoff
        )
    ).first()
    if recent_punch:
        elapsed = int((datetime.utcnow() - recent_punch.timestamp).total_seconds() / 60)
        raise HTTPException(
            status_code=400,
            detail=f"Duplicate punch detected. You already clocked {request.punch_type} {elapsed} minute(s) ago."
        )

    # 5. Record punch using the offline-ready, GPS-validated time from the mobile app
    try:
        # Dart's toIso8601String() for local time omits the offset. Strip 'Z' just in case.
        device_time_str = request.timestamp.replace("Z", "")
        # Remove microsecond precision if present to match Python 3.10- compatibility cleanly
        if "." in device_time_str:
            device_time_str = device_time_str.split(".")[0]
        device_local_time = datetime.fromisoformat(device_time_str)
        
        # Convert device local time back to UTC for standard SQLite storage
        tz_offset = timedelta(minutes=request.tz_offset_minutes)
        server_time_utc = device_local_time - tz_offset
        
        logger.info(f"Using Offline-Ready Time: {device_local_time} (GPS Validated: {request.gps_time_validated})")
    except Exception as e:
        logger.warning(f"Failed to parse device timestamp: {e}. Falling back to server time.")
        server_time_utc = datetime.utcnow()

    log = PunchLog(
        employee_id=effective_employee_id,
        device_uuid=request.device_uuid,
        timestamp=server_time_utc,
        latitude=request.latitude,
        longitude=request.longitude,
        is_mock_location=request.is_mock_location,
        biometric_verified=request.biometric_verified,
        punch_type=request.punch_type,
        tz_offset_minutes=request.tz_offset_minutes,
        adms_status="pending",
        client_punch_id=request.client_punch_id,
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    # 6. Background push to ADMS
    background_tasks.add_task(push_to_adms, log.id, effective_employee_id, server_time_utc, request.punch_type, request.tz_offset_minutes)

    return {
        "status": "success",
        "message": f"Punch recorded: {request.punch_type}",
        "server_time": server_time_utc,
        "log_id": log.id,
    }


# ─── Batch Punch Endpoint ────────────────────────────────────────────────────

@app.post("/api/v1/punch/batch", response_model=BatchPunchResponse)
async def create_batch_punch(
    request: BatchPunchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(verify_api_key),
):
    """Accept up to 50 offline punches in one request for efficient sync."""
    if len(request.punches) > 50:
        raise HTTPException(status_code=400, detail="Batch size limit is 50 punches.")

    results = []
    synced = 0
    failed = 0

    for punch in request.punches:
        try:
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

            # Basic validation (parity with single-punch endpoint)
            if punch.is_mock_location:
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error="Mock location detected. Punch rejected.",
                ))
                failed += 1
                continue
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

            # Duplicate punch prevention (block same punch_type within 5 minutes)
            recent_cutoff = datetime.utcnow() - timedelta(minutes=5)
            recent_punch = db.query(PunchLog).filter(
                and_(
                    PunchLog.employee_id == effective_employee_id,
                    PunchLog.punch_type == punch.punch_type,
                    PunchLog.timestamp >= recent_cutoff
                )
            ).first()
            if recent_punch:
                elapsed = int((datetime.utcnow() - recent_punch.timestamp).total_seconds() / 60)
                results.append(BatchPunchResult(
                    client_punch_id=punch.client_punch_id,
                    status="error",
                    error=f"Duplicate punch detected. Already clocked {punch.punch_type} {elapsed} minute(s) ago.",
                ))
                failed += 1
                continue

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
            )
            db.add(log)
            db.commit()
            db.refresh(log)
            background_tasks.add_task(push_to_adms, log.id, effective_employee_id, server_time_utc, punch.punch_type, punch.tz_offset_minutes)

            results.append(BatchPunchResult(
                client_punch_id=punch.client_punch_id,
                status="success",
                log_id=log.id,
            ))
            synced += 1

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
async def get_punch_types(
    db: Session = Depends(get_db),
    _: ApiKey = Depends(verify_api_key),
):
    """Mobile app fetches available punch types on startup."""
    types = db.query(PunchType).filter(PunchType.is_active == True).order_by(PunchType.display_order).all()
    return [
        PunchTypeResponse(
            code=t.code, label=t.label, adms_status_code=t.adms_status_code,
            display_order=t.display_order, icon=t.icon, color_hex=t.color_hex,
            requires_geofence=t.requires_geofence,
        ) for t in types
    ]


# ─── Punch Log Export ────────────────────────────────────────────────────────

@app.get("/ui/logs/export")
async def export_punch_logs(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Export punch logs as CSV."""
    import csv
    import io
    
    query = db.query(PunchLog, Employee.full_name).\
        outerjoin(Employee, PunchLog.employee_id == Employee.employee_id)
    
    if from_date:
        query = query.filter(PunchLog.timestamp >= datetime.fromisoformat(from_date))
    if to_date:
        query = query.filter(PunchLog.timestamp <= datetime.fromisoformat(to_date))
    
    logs = query.order_by(PunchLog.timestamp.desc()).limit(10000).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Log ID", "Employee", "Employee ID", "Timestamp (UTC)", "Punch Type",
                      "Latitude", "Longitude", "Mock Location", "Biometric",
                      "ADMS Status", "TZ Offset"])
    
    for log, name in logs:
        writer.writerow([
            log.id,
            name or "Unknown",
            log.employee_id,
            log.timestamp.isoformat() if log.timestamp else "",
            log.punch_type,
            log.latitude,
            log.longitude,
            log.is_mock_location,
            log.biometric_verified,
            log.adms_status,
            log.tz_offset_minutes,
        ])
    
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=attendance_export_{datetime.utcnow().strftime('%Y%m%d')}.csv"}
    )


# ─── Health Check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(__import__('sqlalchemy').text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "adms_connected": _handshake_state.get("handshake_done", False),
    }
