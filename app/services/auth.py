"""
Simple API Key authentication for the /api/v1/* endpoints.
Keys are stored in the database as plain strings for simplicity.
In production, consider using hashed keys.
"""
import structlog
import secrets
from datetime import datetime, timedelta
from fastapi import Depends, HTTPException, Security, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.database.models import SessionLocal, ApiKey

logger = structlog.get_logger()

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_api_key(api_key: str = Security(API_KEY_HEADER), db: Session = Depends(get_db), request: Request = None):
    """
    FastAPI dependency that validates the X-API-Key header against the database.
    Raises HTTP 401 if the key is missing or invalid.
    Checks expiry, logs last_used_at / last_used_ip, and warns near expiry.
    """
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API Key. Include 'X-API-Key' header.")

    key_record = db.query(ApiKey).filter(
        ApiKey.key_value == api_key,
        ApiKey.is_active == True
    ).first()

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid or inactive API Key.")

    # ── Expiry check ────────────────────────────────────────────────────────────
    if key_record.expires_at and datetime.utcnow() > key_record.expires_at:
        logger.warning("api_key_expired", label=key_record.label, key_id=key_record.id)
        raise HTTPException(status_code=401, detail="API Key has expired. Please contact admin to issue a new key.")

    # ── Near-expiry warning ────────────────────────────────────────────────────
    if key_record.expires_at:
        days_remaining = (key_record.expires_at - datetime.utcnow()).days
        if 0 <= days_remaining <= 7:
            logger.warning("api_key_near_expiry", label=key_record.label, key_id=key_record.id, days_remaining=days_remaining)

    # ── Update last-used tracking ──────────────────────────────────────────────
    try:
        client_ip = request.client.host if request and hasattr(request, 'client') and request.client else None
    except Exception:
        client_ip = None

    key_record.last_used_at = datetime.utcnow()
    if client_ip:
        key_record.last_used_ip = client_ip
    db.commit()

    logger.info("api_key_used", label=key_record.label, key_id=key_record.id, ip=client_ip)
    return key_record


def generate_api_key() -> str:
    """Generate a secure random API key."""
    return f"atk_{secrets.token_urlsafe(32)}"
