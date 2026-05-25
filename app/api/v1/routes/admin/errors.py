from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.models import SystemErrorLog, AdminUser
from app.services.auth_ui import get_current_admin
from app.api.v1.routes.admin.base import get_db

router = APIRouter(tags=["Admin UI - Error Tracking"])

@router.get("/ui/errors")
async def get_system_errors(
    limit: int = 100,
    offset: int = 0,
    page: Optional[int] = None,
    per_page: Optional[int] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin)
):
    """Fetch unhandled system errors for administrator monitoring and troubleshooting."""
    if getattr(admin, "role", "admin") not in ["superadmin", "admin"]:
        raise HTTPException(status_code=403, detail="Permission denied")

    query = db.query(SystemErrorLog).order_by(SystemErrorLog.created_at.desc())
    if page is not None and per_page is not None:
        query = query.limit(per_page).offset((page - 1) * per_page)
    else:
        query = query.offset(offset).limit(limit)
    
    errors = query.all()
    return [
        {
            "id": e.id,
            "error_message": e.error_message,
            "stack_trace": e.stack_trace,
            "component": e.component,
            "created_at": e.created_at.isoformat() if e.created_at else None
        }
        for e in errors
    ]
