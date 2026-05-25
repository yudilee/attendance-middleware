from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.models import AuditLog, AdminUser
from app.services.auth_ui import get_current_admin
from app.api.v1.schemas import AuditLogResponse
from app.api.v1.routes.admin.base import get_db

router = APIRouter(tags=["Admin UI - Audit Logging"])

@router.get("/ui/audit-logs", response_model=list[AuditLogResponse])
async def get_audit_logs(
    limit: int = 100,
    offset: int = 0,
    page: Optional[int] = None,
    per_page: Optional[int] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    query = db.query(AuditLog).order_by(AuditLog.created_at.desc())
    if page is not None and per_page is not None:
        query = query.limit(per_page).offset((page - 1) * per_page)
    else:
        query = query.offset(offset).limit(limit)
    return query.all()
