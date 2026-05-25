"""Device-related API v1 routes: config, onboarding, selfie upload, FCM token."""
import json
import os
import uuid
import structlog
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from jose import jwt, JWTError

from app.database.models import (
    SessionLocal, DeviceBinding, Branch, BindingBranch,
    Employee, ADMSRegisteredEmployee, ApiKey, PunchLog,
    AppConfig, BranchCheckpoint,
)
from app.services.auth import verify_api_key
from app.services.auth_ui import SECRET_KEY, ALGORITHM, get_current_admin
from app.api.v1.schemas import (
    DeviceConfigResponse, BranchInfo,
    AppStatusResponse, OnboardGenerateRequest, OnboardDeviceRequest,
    CheckpointInfo,
)
from app.cache import get_cache, set_cache
from slowapi import Limiter

logger = structlog.get_logger()

# Selfie upload directory
UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "uploads", "selfies",
)
os.makedirs(UPLOAD_DIR, exist_ok=True)

router = APIRouter(tags=["Device"])

# Rate limiter (shared from main app)
limiter = None


def configure(limiter_ref):
    """Set the limiter reference from the main app."""
    global limiter
    limiter = limiter_ref


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/api/v1/app-status")
async def get_app_status(db: Session = Depends(get_db)):
    """Return app status and minimum version."""
    config = db.query(AppConfig).filter(AppConfig.key == "min_app_version").first()
    min_ver = config.value if config else "1.0.0"
    return AppStatusResponse(status="ok", min_version=min_ver)


@router.get("/api/v1/device-config")
async def get_device_config(
    request: Request,
    device_uuid: str,
    employee_id: str = None,
    device_label: str = None,
    db: Session = Depends(get_db),
    api_key: ApiKey = Depends(verify_api_key),
):
    """
    Mobile app calls this on boot. Auto-registers device if missing.
    Returns registration status and branch config(s) when approved.
    """
    cache_key = f"device_config:{api_key.id}:{device_uuid}"
    cached = await get_cache(cache_key)
    if cached:
        return DeviceConfigResponse(**json.loads(cached))

    # Get max-devices config
    max_cfg = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    max_devices = int(max_cfg.value) if max_cfg else 5

    binding = db.query(DeviceBinding).filter(DeviceBinding.device_uuid == device_uuid).first()

    if not binding:
        # New device registration
        if employee_id:
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
                    employee_id=employee_id,
                )

        binding = DeviceBinding(
            employee_id=employee_id,
            device_uuid=device_uuid,
            device_label=device_label,
            registration_status="pending_approval",
            is_active=True,
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

    # Device count for this employee
    device_count = 0
    employee_name = None
    if binding.employee_id:
        device_count = db.query(DeviceBinding).filter(
            DeviceBinding.employee_id == binding.employee_id,
            DeviceBinding.is_active == True,
            DeviceBinding.registration_status.in_(["approved", "active"]),
        ).count()

        emp = db.query(Employee).filter(Employee.employee_id == binding.employee_id).first()
        if emp and emp.full_name:
            employee_name = emp.full_name
        else:
            adms_emp = db.query(ADMSRegisteredEmployee).filter(ADMSRegisteredEmployee.employee_id == binding.employee_id).first()
            if adms_emp and adms_emp.employee_name:
                employee_name = adms_emp.employee_name

    # Status checks
    status = binding.registration_status
    if status == "pending_approval":
        return DeviceConfigResponse(
            status="pending_approval",
            message="Your device is pending admin approval. Please contact your HR Administrator.",
            device_count=device_count,
            max_devices=max_devices,
            employee_name=employee_name,
            employee_id=binding.employee_id,
        )
    if status == "suspended":
        raise HTTPException(status_code=403, detail="Device suspended. Please contact your HR Administrator.")
    if not binding.is_active:
        raise HTTPException(status_code=403, detail="This device has been deactivated. Contact admin.")

    # Branch assignments
    branch_assignments = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding.id,
    ).all()

    if not branch_assignments:
        return DeviceConfigResponse(
            status="pending_branch",
            message="Device approved. Waiting for branch assignment by admin.",
            device_count=device_count,
            max_devices=max_devices,
            employee_name=employee_name,
            employee_id=binding.employee_id,
        )

    branches = []
    for ba in branch_assignments:
        branch = db.query(Branch).filter(
            Branch.id == ba.branch_id,
            Branch.is_active == True,
        ).first()
        if branch:
            checkpoints_db = db.query(BranchCheckpoint).filter(
                BranchCheckpoint.branch_id == branch.id,
                BranchCheckpoint.is_active == True
            ).all()
            checkpoints = [
                CheckpointInfo(
                    id=cp.id,
                    branch_id=cp.branch_id,
                    name=cp.name,
                    latitude=cp.latitude,
                    longitude=cp.longitude,
                    radius_meters=cp.radius_meters,
                    is_active=cp.is_active,
                    geofence_type=cp.geofence_type,
                    polygon_coordinates=cp.polygon_coordinates,
                    created_at=cp.created_at.isoformat() if cp.created_at else None,
                    updated_at=cp.updated_at.isoformat() if cp.updated_at else None
                )
                for cp in checkpoints_db
            ]
            branches.append(BranchInfo(
                id=branch.id,
                name=branch.name,
                latitude=branch.latitude,
                longitude=branch.longitude,
                radius_meters=branch.radius_meters,
                geofence_type=branch.geofence_type,
                polygon_coordinates=branch.polygon_coordinates,
                qr_code_enabled=branch.qr_code_enabled,
                qr_code_data=branch.qr_code_data if branch.qr_code_enabled else None,
                nfc_enabled=branch.nfc_enabled,
                nfc_tag_data=branch.nfc_tag_data if branch.nfc_enabled else None,
                checkpoints=checkpoints,
            ))

    if not branches:
        return DeviceConfigResponse(
            status="pending_branch",
            message="All assigned branches are inactive.",
            device_count=device_count,
            max_devices=max_devices,
            employee_name=employee_name,
            employee_id=binding.employee_id,
        )

    response = DeviceConfigResponse(
        status="active",
        branches=branches,
        device_count=device_count,
        max_devices=max_devices,
        employee_name=employee_name,
        employee_id=binding.employee_id,
    )
    await set_cache(cache_key, response.model_dump_json(), ttl=300)
    return response


@router.post("/api/v1/punch/selfie")
async def upload_selfie(
    punch_id: int,
    file: UploadFile = File(...),
    api_key=Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Upload a selfie image associated with a punch record."""
    punch = db.query(PunchLog).filter(PunchLog.id == punch_id).first()
    if not punch:
        raise HTTPException(status_code=404, detail="Punch not found")

    allowed_types = ["image/jpeg", "image/png", "image/webp"]
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, and WebP images are allowed")

    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"selfie_{punch_id}_{uuid.uuid4().hex[:8]}.{ext}"
    filepath = os.path.join(UPLOAD_DIR, filename)

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum 5MB allowed")

    with open(filepath, "wb") as f:
        f.write(content)

    punch.selfie_filename = filename
    db.commit()

    return {"status": "success", "filename": filename}


@router.post("/api/v1/device/fcm-token")
async def update_fcm_token(
    token_data: dict,
    api_key=Depends(verify_api_key),
    db: Session = Depends(get_db),
):
    """Register or update FCM token for push notifications."""
    device_uuid = token_data.get("device_uuid")
    fcm_token = token_data.get("fcm_token")
    if not device_uuid or not fcm_token:
        raise HTTPException(status_code=400, detail="device_uuid and fcm_token required")

    binding = db.query(DeviceBinding).filter(DeviceBinding.device_uuid == device_uuid).first()
    if binding:
        binding.fcm_token = fcm_token
        db.commit()
        return {"status": "updated"}
    raise HTTPException(status_code=404, detail="Device not found")


@router.post("/api/v1/admin/generate-onboard-qr")
async def generate_onboard_qr(
    req: OnboardGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """Generates a secure QR payload for device onboarding."""
    payload = {
        "emp": req.employee_id,
        "branch": req.branch_id,
        "key_id": req.api_key_id,
        "exp": datetime.utcnow() + timedelta(hours=24),
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    config = db.query(AppConfig).filter(AppConfig.key == "server_url").first()
    if config and config.value and "localhost" not in config.value and "127.0.0.1" not in config.value:
        server_url = config.value
    else:
        # Dynamically determine the URL from the request host, respecting reverse proxy headers (e.g. Traefik)
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.url.netloc)
        server_url = f"{scheme}://{host}"

    return {"url": server_url, "token": token}


@router.post("/api/v1/device-onboard")
async def onboard_device(
    request: Request,
    req: OnboardDeviceRequest,
    db: Session = Depends(get_db),
):
    """Mobile app uses this with the token to auto-approve device."""
    try:
        payload = jwt.decode(req.token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid or expired onboarding token")

    employee_id = payload.get("emp")
    branch_id = payload.get("branch")
    api_key_id = payload.get("key_id")

    if not employee_id or not branch_id or not api_key_id:
        raise HTTPException(status_code=400, detail="Invalid token payload")

    max_cfg = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    max_devices = int(max_cfg.value) if max_cfg else 5

    import secrets
    binding = db.query(DeviceBinding).filter(DeviceBinding.device_uuid == req.device_uuid).first()
    if not binding:
        existing_count = db.query(DeviceBinding).filter(
            DeviceBinding.employee_id == employee_id,
            DeviceBinding.is_active == True,
            DeviceBinding.registration_status.in_(["approved", "active"]),
        ).count()
        if existing_count >= max_devices:
            raise HTTPException(status_code=400, detail="Maximum devices reached")

        device_secret = f"sec_{secrets.token_hex(32)}"
        binding = DeviceBinding(
            employee_id=employee_id,
            device_uuid=req.device_uuid,
            device_label=req.device_label,
            registration_status="active",
            is_active=True,
            api_key_id=api_key_id,
            approved_at=datetime.utcnow(),
            approved_by="System (QR)",
            device_secret=device_secret,
        )
        db.add(binding)
        db.commit()
        db.refresh(binding)
    else:
        if not binding.device_secret:
            binding.device_secret = f"sec_{secrets.token_hex(32)}"
        binding.employee_id = employee_id
        binding.registration_status = "active"
        binding.api_key_id = api_key_id
        binding.is_active = True
        binding.approved_at = datetime.utcnow()
        binding.approved_by = "System (QR)"
        db.commit()

    # Broadcast WebSocket event to notify admin dashboard of new registration
    try:
        from app.api.v1.routes.punch import manager as ws_manager
        onboard_event = json.dumps({
            "type": "device_onboarded",
            "device_uuid": binding.device_uuid,
            "employee_id": employee_id,
            "employee_name": employee_name or "",
            "status": "active",
            "timestamp": datetime.utcnow().isoformat(),
        })
        import asyncio
        await ws_manager.broadcast(onboard_event)
    except Exception:
        pass  # Non-critical, don't fail registration if WebSocket fails

    # Assign branch
    existing_branch = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding.id,
        BindingBranch.branch_id == branch_id,
    ).first()
    if not existing_branch:
        db.add(BindingBranch(binding_id=binding.id, branch_id=branch_id))
        db.commit()

    # Build response
    api_key = db.query(ApiKey).filter(ApiKey.id == api_key_id).first()

    employee_name = None
    emp = db.query(Employee).filter(Employee.employee_id == employee_id).first()
    if emp and emp.full_name:
        employee_name = emp.full_name
    else:
        adms_emp = db.query(ADMSRegisteredEmployee).filter(ADMSRegisteredEmployee.employee_id == employee_id).first()
        if adms_emp and adms_emp.employee_name:
            employee_name = adms_emp.employee_name

    branches = []
    branch = db.query(Branch).filter(Branch.id == branch_id, Branch.is_active == True).first()
    if branch:
        checkpoints_db = db.query(BranchCheckpoint).filter(
            BranchCheckpoint.branch_id == branch.id,
            BranchCheckpoint.is_active == True
        ).all()
        checkpoints = [
            CheckpointInfo(
                id=cp.id,
                branch_id=cp.branch_id,
                name=cp.name,
                latitude=cp.latitude,
                longitude=cp.longitude,
                radius_meters=cp.radius_meters,
                is_active=cp.is_active,
                created_at=cp.created_at.isoformat() if cp.created_at else None,
                updated_at=cp.updated_at.isoformat() if cp.updated_at else None
            )
            for cp in checkpoints_db
        ]
        branches.append(BranchInfo(
            id=branch.id,
            name=branch.name,
            latitude=branch.latitude,
            longitude=branch.longitude,
            radius_meters=branch.radius_meters,
            qr_code_enabled=branch.qr_code_enabled,
            qr_code_data=branch.qr_code_data if branch.qr_code_enabled else None,
            nfc_enabled=branch.nfc_enabled,
            nfc_tag_data=branch.nfc_tag_data if branch.nfc_enabled else None,
            checkpoints=checkpoints,
        ))

    device_count = db.query(DeviceBinding).filter(
        DeviceBinding.employee_id == employee_id,
        DeviceBinding.is_active == True,
        DeviceBinding.registration_status.in_(["approved", "active"]),
    ).count()

    resp = DeviceConfigResponse(
        status="active",
        branches=branches,
        device_count=device_count,
        max_devices=max_devices,
        employee_name=employee_name,
        employee_id=employee_id,
    ).model_dump()

    resp["api_key"] = api_key.key_value if api_key else ""
    resp["device_secret"] = binding.device_secret or ""
    return resp
