import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request
from pydantic import BaseModel
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import Response

from app.services.auth import verify_api_key, generate_api_key
from app.services.auth_ui import get_password_hash, verify_password, create_access_token, get_current_admin

from app.database.models import init_db, SessionLocal, DeviceBinding, PunchLog, ADMSTarget, Branch, ApiKey, ADMSRegisteredEmployee, AdminUser
from app.api.v1.schemas import PunchRequest, PunchResponse, DeviceConfigResponse
from app.services.geo import is_within_fence
from app.services.adms_service import push_to_adms, adms_heartbeat_loop, retry_failed_pushes, get_adms_config, test_adms_connection

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


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
    yield
    # Graceful shutdown
    heartbeat_task.cancel()
    retry_task.cancel()
    try:
        await asyncio.gather(heartbeat_task, retry_task, return_exceptions=True)
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
    punch_count = db.query(PunchLog).count()
    device_count = db.query(DeviceBinding).count()
    uploaded_count = db.query(PunchLog).filter(PunchLog.adms_status == "uploaded").count()
    failed_count = db.query(PunchLog).filter(PunchLog.adms_status == "failed").count()
    pending_count = db.query(PunchLog).filter(PunchLog.adms_status == "pending").count()
    logs = db.query(PunchLog).order_by(PunchLog.timestamp.desc()).limit(20).all()
    devices = db.query(DeviceBinding).all()

    server_url, sn, device_name = get_adms_config()

    # Branches
    branches = db.query(Branch).all()

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
            "branches": branches,
            "admin": admin,
        }
    )

class ADMSConfigRequest(BaseModel):
    server_url: str
    serial_number: str
    device_name: str

@app.post("/ui/settings")
async def update_settings(config: ADMSConfigRequest, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    target = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
    if not target:
        target = ADMSTarget()
        db.add(target)

    target.server_url = config.server_url
    target.serial_number = config.serial_number
    target.device_name = config.device_name
    db.commit()
    logger.info(f"ADMS Config updated: {config.server_url} SN={config.serial_number} Alias={config.device_name}")
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
    
    admin.hashed_password = get_password_hash(req.new_password)
    db.commit()
    logger.info(f"Admin '{admin.username}' updated their password.")
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
    success, message = await test_adms_connection(config.server_url, config.serial_number)
    return {"success": success, "message": message}


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
    else:
        binding.branch_id = int(branch_id)
        
    db.commit()
    logger.info(f"Assigned device {employee_id} to branch {branch_id}")
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
    """List all API keys (values truncated for security)."""
    keys = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    return [
        {
            "id": k.id,
            "label": k.label,
            "key_preview": k.key_value[:12] + "...",
            "is_active": k.is_active,
            "created_at": k.created_at,
            "last_used_at": k.last_used_at,
        }
        for k in keys
    ]


@app.delete("/ui/api-keys/{key_id}")
async def revoke_api_key(key_id: int, db: Session = Depends(get_db), admin: AdminUser = Depends(get_current_admin)):
    """Revoke (deactivate) an API key."""
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if key:
        key.is_active = False
        db.commit()
    return {"status": "revoked"}


# ─── API V1 ROUTES ───────────────────────────────────────────────────────────

@app.get("/api/v1/device-config", response_model=DeviceConfigResponse)
async def get_device_config(
    device_uuid: str, 
    employee_id: str, 
    db: Session = Depends(get_db),
    _: ApiKey = Depends(verify_api_key)
):
    """
    Mobile app calls this on boot. 
    Auto-registers device if missing. 
    Returns pending status or assigned branch config.
    """
    binding = db.query(DeviceBinding).filter(DeviceBinding.device_uuid == device_uuid).first()
    if not binding:
        binding = DeviceBinding(employee_id=employee_id, device_uuid=device_uuid)
        db.add(binding)
        db.commit()
        db.refresh(binding)
    
    if not binding.branch_id:
        return DeviceConfigResponse(status="pending")
    
    assigned_branch = db.query(Branch).filter(Branch.id == binding.branch_id).first()
    if not assigned_branch or not assigned_branch.is_active:
        return DeviceConfigResponse(status="pending")
        
    return DeviceConfigResponse(
        status="assigned",
        branch_name=assigned_branch.name,
        latitude=assigned_branch.latitude,
        longitude=assigned_branch.longitude,
        radius_meters=assigned_branch.radius_meters
    )


@app.post("/api/v1/punch", response_model=PunchResponse)
async def create_punch(
    request: PunchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(verify_api_key),   # Enforce API key auth
):
    # 1. Validation
    if request.is_mock_location:
        raise HTTPException(status_code=400, detail="Mock location detected. Punch rejected.")
    if not request.biometric_verified:
        raise HTTPException(status_code=400, detail="Biometric verification failed.")

    # 2. Device binding and Branch assignment check
    binding = db.query(DeviceBinding).filter(DeviceBinding.employee_id == request.employee_id).first()
    if not binding:
        binding = DeviceBinding(employee_id=request.employee_id, device_uuid=request.device_uuid)
        db.add(binding)
        db.commit()
    else:
        if binding.device_uuid != request.device_uuid:
            raise HTTPException(status_code=400, detail="Unauthorized device. Use your registered handset.")
            
    if not binding.branch_id:
        raise HTTPException(status_code=403, detail="Device not assigned to any branch. Please contact Admin.")
        
    assigned_branch = db.query(Branch).filter(Branch.id == binding.branch_id).first()

    # 3. Geofencing check
    in_fence, distance = is_within_fence(request.latitude, request.longitude, assigned_branch)
    if not in_fence:
        raise HTTPException(status_code=400, detail=f"Outside assigned branch ({distance:.0f}m away). Must be within fence.")

    # 4. Duplicate punch prevention (block same punch_type within 5 minutes)
    from sqlalchemy import and_
    from datetime import timedelta
    recent_cutoff = datetime.utcnow() - timedelta(minutes=5)
    recent_punch = db.query(PunchLog).filter(
        and_(
            PunchLog.employee_id == request.employee_id,
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
        employee_id=request.employee_id,
        device_uuid=request.device_uuid,
        timestamp=server_time_utc,
        latitude=request.latitude,
        longitude=request.longitude,
        is_mock_location=request.is_mock_location,
        biometric_verified=request.biometric_verified,
        punch_type=request.punch_type,
        adms_status="pending",
    )
    db.add(log)
    db.commit()
    db.refresh(log)

    # 6. Background push to ADMS (now with log_id for status tracking)
    background_tasks.add_task(push_to_adms, log.id, request.employee_id, server_time_utc, request.punch_type)

    return {
        "status": "success",
        "message": f"Punch recorded: {request.punch_type}",
        "server_time": server_time_utc,
        "log_id": log.id
    }
