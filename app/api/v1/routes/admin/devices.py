from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database.models import DeviceBinding, BindingBranch, AppConfig, AdminUser, Branch, Employee, ADMSRegisteredEmployee, ApiKey
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import DeviceLabelRequest
from app.cache import invalidate_cache
from app.api.v1.routes.admin.base import get_db, log_audit_action

router = APIRouter(tags=["Admin UI - Device Management"])

MAX_DEVICES_DEFAULT = 5

def _get_max_devices(db):
    max_cfg = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    return int(max_cfg.value) if max_cfg else MAX_DEVICES_DEFAULT

def _serialize_device(binding, db, max_devices):
    """Serialize a DeviceBinding row to a dict for the device list."""
    # Employee info
    employee_name = None
    if binding.employee_id:
        emp = db.query(Employee).filter(Employee.employee_id == binding.employee_id).first()
        if emp and emp.full_name:
            employee_name = emp.full_name
        else:
            adms_emp = db.query(ADMSRegisteredEmployee).filter(ADMSRegisteredEmployee.employee_id == binding.employee_id).first()
            if adms_emp and adms_emp.employee_name:
                employee_name = adms_emp.employee_name
    
    # Device count for this employee
    device_count = 0
    if binding.employee_id:
        device_count = db.query(DeviceBinding).filter(
            DeviceBinding.employee_id == binding.employee_id,
            DeviceBinding.is_active == True,
            DeviceBinding.registration_status.in_(["approved", "active"]),
        ).count()
    
    # Branch list
    branch_assignments = db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).all()
    branch_list = []
    for ba in branch_assignments:
        branch = db.query(Branch).filter(Branch.id == ba.branch_id).first()
        if branch:
            branch_list.append({"id": branch.id, "name": branch.name})
    
    # API key info
    api_key_label = ""
    api_key_preview = ""
    is_unique_key = False
    if binding.api_key_id:
        api_key = db.query(ApiKey).filter(ApiKey.id == binding.api_key_id).first()
        if api_key:
            api_key_label = api_key.label
            api_key_preview = api_key.key_value[:20] + "..." if len(api_key.key_value) > 20 else api_key.key_value
            # Check if this key is used by only one device (unique key from QR onboarding)
            key_device_count = db.query(DeviceBinding).filter(
                DeviceBinding.api_key_id == binding.api_key_id,
                DeviceBinding.is_active == True,
            ).count()
            is_unique_key = key_device_count == 1
    
    return {
        "id": binding.id,
        "employee_id": binding.employee_id or "",
        "employee_name": employee_name or "",
        "device_label": binding.device_label or "",
        "device_uuid": binding.device_uuid,
        "device_count": device_count,
        "max_devices": max_devices,
        "is_active": binding.is_active,
        "registration_status": binding.registration_status or "pending_approval",
        "branch_list": branch_list,
        "api_key_label": api_key_label,
        "api_key_preview": api_key_preview,
        "is_unique_key": is_unique_key,
        "created_at": binding.created_at.isoformat() if binding.created_at else None,
    }

@router.get("/ui/devices/list")
async def get_devices_list(
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """Return device bindings as JSON for AJAX refresh."""
    max_devices = _get_max_devices(db)
    bindings = db.query(DeviceBinding).order_by(DeviceBinding.created_at.desc()).all()
    return [_serialize_device(b, db, max_devices) for b in bindings]

@router.post("/ui/devices/{binding_id}/approve")
async def approve_device(
    binding_id: int,
    request: Request,
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
    log_audit_action(db, admin.username, "approve_device", "device_binding", binding_id, f"Approved device {binding.device_label or binding.device_uuid} for employee {binding.employee_id}", request)
    return {"status": "approved"}

@router.post("/ui/devices/{binding_id}/suspend")
async def suspend_device(
    binding_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device not found")
    binding.registration_status = "suspended"
    db.commit()
    await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    log_audit_action(db, admin.username, "suspend_device", "device_binding", binding_id, f"Suspended device {binding.device_label or binding.device_uuid}", request)
    return {"status": "suspended"}

@router.put("/ui/devices/{binding_id}/label")
async def update_device_label(
    binding_id: int,
    req: DeviceLabelRequest,
    request: Request,
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
    log_audit_action(db, admin.username, "update_device_label", "device_binding", binding_id, f"Updated label of device {binding.device_uuid} to {req.label}", request)
    return {"status": "updated"}

@router.post("/ui/devices/{binding_id}/set-active")
async def set_active_device(
    binding_id: int,
    request: Request,
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
    log_audit_action(db, admin.username, "set_active_device", "device_binding", binding_id, f"Set device {binding.device_label or binding.device_uuid} as active for employee {binding.employee_id}", request)
    return {"status": "updated"}

@router.delete("/ui/devices/{binding_id}/unbind")
async def unbind_device(
    binding_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if binding:
        device_name = binding.device_label or binding.device_uuid
        employee_id = binding.employee_id
        api_key_id = binding.api_key_id
        await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
        
        # Revoke the API key associated with this device
        if api_key_id:
            api_key = db.query(ApiKey).filter(ApiKey.id == api_key_id).first()
            if api_key:
                # Check if this key is used by any other active device
                other_device = db.query(DeviceBinding).filter(
                    DeviceBinding.api_key_id == api_key_id,
                    DeviceBinding.id != binding_id,
                    DeviceBinding.is_active == True,
                    DeviceBinding.registration_status.in_(["approved", "active"]),
                ).first()
                if not other_device:
                    # No other device uses this key, revoke it
                    api_key.is_active = False
                    db.commit()
        
        db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).delete()
        db.delete(binding)
        db.commit()
        log_audit_action(db, admin.username, "unbind_device", "device_binding", binding_id, f"Unbound/Deleted device {device_name} from employee {employee_id}", request)
    return {"status": "success"}

@router.post("/ui/devices/{binding_id}/bind-branch")
async def bind_device_to_branch(
    binding_id: int,
    req: dict,
    request: Request,
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
        details = f"Unlinked all branches from device {binding.device_label or binding.device_uuid}"
    else:
        binding.branch_id = int(branch_id)
        existing = db.query(BindingBranch).filter(
            BindingBranch.binding_id == binding.id,
            BindingBranch.branch_id == int(branch_id),
        ).first()
        if not existing:
            db.add(BindingBranch(binding_id=binding.id, branch_id=int(branch_id)))
        details = f"Bound device {binding.device_label or binding.device_uuid} to branch ID {branch_id}"
    db.commit()
    await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    log_audit_action(db, admin.username, "bind_device_branch", "device_binding", binding_id, details, request)
    return {"status": "success"}

@router.post("/ui/devices/{binding_id}/assign")
async def assign_device_employee(
    binding_id: int,
    employee_id: str,
    request: Request,
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
    log_audit_action(db, admin.username, "assign_device_employee", "device_binding", binding_id, f"Assigned device {binding.device_label or binding.device_uuid} to employee {employee_id}", request)
    return {"status": "success"}
