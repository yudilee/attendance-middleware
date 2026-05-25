from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.models import AdminUser
from app.services.auth_ui import get_current_admin, get_password_hash
from app.api.v1.schemas import CreateUserRequest
from app.api.v1.routes.admin.base import get_db

router = APIRouter(tags=["Admin UI - User Management"])

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
