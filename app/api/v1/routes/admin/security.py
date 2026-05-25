import hashlib
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.models import ApiKey, DeviceBinding, AdminUser
from app.services.auth import generate_api_key, hash_api_key
from app.services.auth_ui import get_current_admin
from app.api.v1.routes.admin.base import get_db

router = APIRouter(tags=["Admin UI - Security Management"])

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

@router.post("/ui/migrate-api-keys")
async def run_api_key_migration(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    keys = db.query(ApiKey).all()
    count = 0
    for key in keys:
        if not key.key_value.startswith("sha256:"):
            if key.key_value.startswith("atk_"):
                hashed = hashlib.sha256(key.key_value.encode("utf-8")).hexdigest()
                key.key_value = "sha256:" + hashed
            elif len(key.key_value) == 64:
                key.key_value = "sha256:" + key.key_value
            else:
                hashed = hashlib.sha256(key.key_value.encode("utf-8")).hexdigest()
                key.key_value = "sha256:" + hashed
            count += 1
    if count > 0:
        db.commit()
    return {"status": "success", "migrated_count": count}
