import structlog
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database.models import (
    Branch, BranchCheckpoint, DeviceBinding, BindingBranch, Employee, AdminUser
)
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import BranchRequest, CheckpointCreate, CheckpointUpdate
from app.cache import invalidate_cache
from app.api.v1.routes.admin.base import get_db, log_audit_action

logger = structlog.get_logger()

router = APIRouter(tags=["Admin UI - Branch & Checkpoint Management"])

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
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    binding = db.query(DeviceBinding).filter(DeviceBinding.id == binding_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="Device binding not found")
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
        
    if binding.employee_id:
        employee = db.query(Employee).filter(Employee.employee_id == binding.employee_id).first()
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
    log_audit_action(db, admin.username, "assign_branch_device", "device_binding", binding_id, f"Linked branch {branch.name} to device {binding.device_label or binding.device_uuid}", request)
    return {"status": "success"}

@router.delete("/ui/devices/{binding_id}/branches/{branch_id}")
async def remove_branch_from_device(
    binding_id: int,
    branch_id: int,
    request: Request,
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
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    db.delete(assignment)
    db.commit()
    if binding:
        await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
    branch_name = branch.name if branch else f"ID {branch_id}"
    device_label = binding.device_label or binding.device_uuid if binding else f"ID {binding_id}"
    log_audit_action(db, admin.username, "remove_branch_device", "device_binding", binding_id, f"Removed branch {branch_name} from device {device_label}", request)
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
            "timezone_name": getattr(b, "timezone_name", "Asia/Jakarta") or "Asia/Jakarta",
        })
    return result

@router.post("/ui/branches")
async def create_branch(
    req: BranchRequest,
    request: Request,
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
        timezone_name=req.timezone_name or "Asia/Jakarta",
    )
    db.add(new_branch)
    db.commit()
    db.refresh(new_branch)
    log_audit_action(db, admin.username, "created_branch", "branch", new_branch.id, f"Created branch {new_branch.name}", request)
    return {"status": "success", "id": new_branch.id}

@router.put("/ui/branches/{branch_id}")
async def update_branch(
    branch_id: int,
    req: BranchRequest,
    request: Request,
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
    branch.timezone_name = req.timezone_name or "Asia/Jakarta"
    db.commit()
    from app.services.cached_lookups import invalidate_branch_cache
    invalidate_branch_cache(branch_id)
    affected_bindings = db.query(DeviceBinding).outerjoin(
        BindingBranch, DeviceBinding.id == BindingBranch.binding_id
    ).filter(
        (DeviceBinding.branch_id == branch_id) | (BindingBranch.branch_id == branch_id)
    ).all()
    for ab in affected_bindings:
        await invalidate_cache(f"device_config:{ab.api_key_id}:{ab.device_uuid}")
    log_audit_action(db, admin.username, "updated_branch", "branch", branch.id, f"Updated branch {branch.name}", request)
    return {"status": "success"}

@router.delete("/ui/branches/{branch_id}")
async def delete_branch(
    branch_id: int,
    request: Request,
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
    branch_name = branch.name
    db.delete(branch)
    db.commit()
    from app.services.cached_lookups import invalidate_branch_cache
    invalidate_branch_cache(branch_id)
    log_audit_action(db, admin.username, "deleted_branch", "branch", branch_id, f"Deleted branch {branch_name}", request)
    return {"status": "success"}

@router.patch("/ui/branches/{branch_id}/toggle")
async def toggle_branch_status(
    branch_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    branch.is_active = not branch.is_active
    db.commit()
    from app.services.cached_lookups import invalidate_branch_cache
    invalidate_branch_cache(branch_id)
    affected_bindings = db.query(DeviceBinding).outerjoin(
        BindingBranch, DeviceBinding.id == BindingBranch.binding_id
    ).filter(
        (DeviceBinding.branch_id == branch_id) | (BindingBranch.branch_id == branch_id)
    ).all()
    for ab in affected_bindings:
        await invalidate_cache(f"device_config:{ab.api_key_id}:{ab.device_uuid}")
    log_audit_action(db, admin.username, "toggle_branch_active", "branch", branch.id, f"Toggled branch {branch.name} is_active to {branch.is_active}", request)
    return {"status": "success", "is_active": branch.is_active}

# Checkpoint Endpoints
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
    request: Request,
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
    log_audit_action(db, admin.username, "created_checkpoint", "branch_checkpoint", cp.id, f"Created checkpoint {cp.name} in branch {branch.name}", request)
    return {"status": "created", "id": cp.id}

@router.put("/ui/branches/{branch_id}/checkpoints/{checkpoint_id}")
async def update_checkpoint(
    branch_id: int,
    checkpoint_id: int,
    req: CheckpointUpdate,
    request: Request,
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
    log_audit_action(db, admin.username, "updated_checkpoint", "branch_checkpoint", cp.id, f"Updated checkpoint {cp.name}", request)
    return {"status": "updated"}

@router.delete("/ui/branches/{branch_id}/checkpoints/{checkpoint_id}")
async def delete_checkpoint(
    branch_id: int,
    checkpoint_id: int,
    request: Request,
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

    checkpoint_name = cp.name
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
    log_audit_action(db, admin.username, "deleted_checkpoint", "branch_checkpoint", checkpoint_id, f"Deleted checkpoint {checkpoint_name}", request)
    return {"status": "deleted"}

@router.patch("/ui/branches/{branch_id}/checkpoints/{checkpoint_id}/toggle")
async def toggle_checkpoint(
    branch_id: int,
    checkpoint_id: int,
    request: Request,
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
    log_audit_action(db, admin.username, "toggle_checkpoint_active", "branch_checkpoint", cp.id, f"Toggled checkpoint {cp.name} is_active to {cp.is_active}", request)
    return {"status": "success", "is_active": cp.is_active}
