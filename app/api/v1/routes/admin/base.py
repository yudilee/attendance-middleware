import os
from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database.models import SessionLocal, AuditLog

# Setup templates directory absolute path
current_dir = os.path.dirname(os.path.abspath(__file__))
# routes/admin -> routes -> v1 -> api -> app -> templates
app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_dir))))
template_path = os.path.join(app_dir, "templates")
templates = Jinja2Templates(directory=template_path)

# Global ARQ pool reference
arq_pool = None

def configure(arq_pool_ref):
    """Configure the shared ARQ pool reference for all admin sub-modules."""
    global arq_pool
    arq_pool = arq_pool_ref

def get_arq_pool():
    return arq_pool

def get_db():
    """Dependency to get a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def log_audit_action(
    db: Session,
    admin_username: str,
    action: str,
    target_type: str = None,
    target_id: str = None,
    details: str = None,
    request: Request = None
):
    """Helper to log administrative actions to the audit log."""
    ip_address = None
    if request:
        ip_address = request.client.host
    log_entry = AuditLog(
        admin_username=admin_username,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
        details=details,
        ip_address=ip_address
    )
    db.add(log_entry)
    db.commit()
