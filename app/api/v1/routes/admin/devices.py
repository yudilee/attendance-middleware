from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database.models import DeviceBinding, BindingBranch, AppConfig, AdminUser
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import DeviceLabelRequest
from app.cache import invalidate_cache
from app.api.v1.routes.admin.base import get_db, log_audit_action

router = APIRouter(tags=["Admin UI - Device Management"])

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
        await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
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
