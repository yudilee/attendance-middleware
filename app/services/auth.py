"""
Simple API Key authentication for the /api/v1/* endpoints.
Keys are stored in the database as plain strings for simplicity.
In production, consider using hashed keys.
"""
import logging
import secrets
from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.database.models import SessionLocal, ApiKey

logger = logging.getLogger(__name__)

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_api_key(api_key: str = Security(API_KEY_HEADER), db: Session = Depends(get_db)):
    """
    FastAPI dependency that validates the X-API-Key header against the database.
    Raises HTTP 401 if the key is missing or invalid.
    """
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API Key. Include 'X-API-Key' header.")

    key_record = db.query(ApiKey).filter(
        ApiKey.key_value == api_key,
        ApiKey.is_active == True
    ).first()

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid or inactive API Key.")

    return key_record


def generate_api_key() -> str:
    """Generate a secure random API key."""
    return f"atk_{secrets.token_urlsafe(32)}"
