"""
Shared punch validation and recording logic.

Extracted from main.py to eliminate ~200 lines of duplicated validation
between the single-punch and batch-punch endpoints.
"""
import structlog
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from app.database.models import (
    DeviceBinding, PunchLog, BindingBranch, Branch,
    PunchType, AppConfig
)
from app.api.v1.schemas import PunchRequest
from app.services.geo import is_within_any_fence
from app.config import settings

logger = structlog.get_logger()


class PunchValidationError(Exception):
    """Raised when a punch fails validation. Contains a user-facing message and HTTP status code."""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def validate_and_prepare_punch(
    db: Session,
    punch: PunchRequest,
    binding: Optional[DeviceBinding] = None,
) -> dict:
    """
    Validate a punch request and prepare the data for insertion.
    
    Steps performed:
    1. Idempotency check (client_punch_id)
    2. Basic validation (biometric)
    3. Punch type validation
    4. Device binding validation (or use provided binding)
    5. Branch assignment + geofencing
    6. Duplicate detection (5-min window)
    7. Daily rate limit
    8. Notes generation (mock location, GPS flags)
    9. Timestamp parsing
    10. Employee ID resolution (from binding)
    
    Returns a dict with keys suitable for creating a PunchLog.
    Raises PunchValidationError on any failure.
    """
    # ── 1. Idempotency check ────────────────────────────────────────────
    if punch.client_punch_id:
        existing = db.query(PunchLog).filter(
            PunchLog.client_punch_id == punch.client_punch_id
        ).first()
        if existing:
            raise PunchValidationError(
                f"Punch already recorded (duplicate): {existing.punch_type}",
                status_code=200,  # Not an error — return existing
            )

    # ── 2. Basic validation ─────────────────────────────────────────────
    if not punch.biometric_verified:
        raise PunchValidationError("Biometric verification failed.")

    # ── 3. Device binding check ────────────────────────────────────────
    if binding is None:
        binding = db.query(DeviceBinding).filter(
            DeviceBinding.device_uuid == punch.device_uuid
        ).first()

    if not binding:
        raise PunchValidationError(
            "Device not registered. Please register in the app settings first.",
            status_code=403,
        )
    if not binding.employee_id:
        raise PunchValidationError(
            "Device not yet assigned to an employee by administrator.",
            status_code=403,
        )

    effective_employee_id = binding.employee_id

    if binding.registration_status == "pending_approval":
        raise PunchValidationError("Device pending admin approval.", status_code=403)
    if binding.registration_status == "suspended":
        raise PunchValidationError("Device suspended. Contact admin.", status_code=403)
    if not binding.is_active:
        raise PunchValidationError("Device has been deactivated. Contact admin.", status_code=403)

    # ── 4. Punch type resolution & validation ────────────────────────────
    requested_type = (punch.punch_type or "auto").strip().lower()
    resolved_type = requested_type

    # Smart sequence resolution: inspect employee's latest punch today
    tz_offset = timedelta(minutes=punch.tz_offset_minutes if punch.tz_offset_minutes is not None else 420)
    device_local_now = datetime.utcnow() + tz_offset
    today_start_local = datetime(device_local_now.year, device_local_now.month, device_local_now.day)
    today_start_utc = today_start_local - tz_offset
    today_end_utc = today_start_utc + timedelta(days=1)

    latest_today_punch = db.query(PunchLog).filter(
        PunchLog.employee_id == effective_employee_id,
        PunchLog.timestamp >= today_start_utc,
        PunchLog.timestamp < today_end_utc
    ).order_by(PunchLog.timestamp.desc()).first()

    if requested_type in ["auto", "in"]:
        if latest_today_punch:
            last_type = (latest_today_punch.punch_type or "").strip().lower()
            time_since_last = (datetime.utcnow() - (latest_today_punch.timestamp or datetime.utcnow())).total_seconds()
            if last_type in ["in", "check in"]:
                # If user already clocked in today and is punching again (after 2 min grace period), auto-resolve to 'out'
                if time_since_last >= 120 or requested_type == "auto":
                    resolved_type = "out"
                else:
                    resolved_type = "in"
            elif last_type in ["out", "check out"]:
                resolved_type = "in"
            else:
                resolved_type = "in"
        else:
            resolved_type = "in"

    valid_type = db.query(PunchType).filter(
        PunchType.code == resolved_type,
        PunchType.is_active == True
    ).first()

    if not valid_type:
        # Fallback to standard check
        valid_type = db.query(PunchType).filter(
            PunchType.code == punch.punch_type,
            PunchType.is_active == True
        ).first()
        if not valid_type:
            resolved_type = "in" if resolved_type == "auto" else punch.punch_type

    effective_punch_type = resolved_type.capitalize() if resolved_type in ["in", "out"] else resolved_type

    # ── HMAC Request Signature Verification ──
    if punch.signature:
        if not binding.device_secret:
            raise PunchValidationError("Device secret not configured for signing.", status_code=403)
        import hmac
        import hashlib
        payload_string = (
            f"{punch.employee_id or ''}:"
            f"{punch.device_uuid}:"
            f"{punch.timestamp}:"
            f"{punch.punch_type}:"
            f"{punch.client_punch_id or ''}"
        )
        expected = hmac.new(binding.device_secret.encode('utf-8'), payload_string.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(punch.signature, expected):
            raise PunchValidationError("Invalid request signature.", status_code=403)

    # ── 5. Branch assignment + geofencing ──────────────────────────────
    branch_assignments = db.query(BindingBranch).filter(
        BindingBranch.binding_id == binding.id,
    ).all()
    if not branch_assignments:
        raise PunchValidationError(
            "Device not assigned to any branch. Please contact Admin.",
            status_code=403,
        )

    assigned_branches = []
    for ba in branch_assignments:
        branch = db.query(Branch).filter(
            Branch.id == ba.branch_id,
            Branch.is_active == True,
        ).first()
        if branch:
            assigned_branches.append(branch)

    if not assigned_branches:
        raise PunchValidationError(
            "All assigned branches are inactive. Please contact Admin.",
            status_code=403,
        )

    # Check geofence (skip if punch type doesn't require it)
    if valid_type.requires_geofence:
        in_fence, distance, best_branch = is_within_any_fence(
            punch.latitude, punch.longitude, assigned_branches, db=db
        )
        if not in_fence:
            raise PunchValidationError(
                f"Outside assigned branches. Nearest: {best_branch} ({distance:.0f}m away).",
                status_code=403,
            )
    else:
        in_fence = True
        distance = 0.0
        best_branch = assigned_branches[0].name if assigned_branches else None

    # ── 6. Duplicate detection (5-min window) ──────────────────────────
    recent_cutoff = datetime.utcnow() - timedelta(minutes=5)
    recent_punch = db.query(PunchLog).filter(
        and_(
            PunchLog.employee_id == effective_employee_id,
            PunchLog.punch_type == punch.punch_type,
            PunchLog.timestamp >= recent_cutoff,
        )
    ).first()
    if recent_punch:
        raise PunchValidationError(
            f"Duplicate {punch.punch_type} detected within 5 minutes",
            status_code=409,
        )

    # ── 7. Daily rate limit ────────────────────────────────────────────
    MAX_DAILY_PUNCHES = settings.max_daily_punches
    daily_count = db.query(PunchLog).filter(
        PunchLog.employee_id == effective_employee_id,
        func.date(PunchLog.timestamp) == func.current_date()
    ).count()
    if daily_count >= MAX_DAILY_PUNCHES:
        raise PunchValidationError("Maximum daily punches exceeded.")

    # ── 8. Notes generation ────────────────────────────────────────────
    notes_parts = []
    if punch.is_mock_location:
        notes_parts.append("mock_location: client-reported")
    if not punch.gps_time_validated:
        notes_parts.append("gps_time_validated: client-reported")
    notes = "; ".join(notes_parts) if notes_parts else None

    # ── 9. Timestamp parsing ───────────────────────────────────────────
    try:
        device_time_str = punch.timestamp.replace("Z", "")
        if "." in device_time_str:
            device_time_str = device_time_str.split(".")[0]
        device_local_time = datetime.fromisoformat(device_time_str)
        tz_offset = timedelta(minutes=punch.tz_offset_minutes)
        server_time_utc = device_local_time - tz_offset

        # Time deviation check
        time_diff = abs((datetime.utcnow() - server_time_utc).total_seconds())
        if time_diff > settings.max_timestamp_deviation_seconds:
            raise PunchValidationError(
                "Timestamp deviation too large. Please sync your device time.",
                status_code=422,
            )
    except PunchValidationError:
        raise
    except Exception:
        logger.warning("Failed to parse device timestamp, falling back to UTC.")
        server_time_utc = datetime.utcnow()

    return {
        "employee_id": effective_employee_id,
        "device_uuid": punch.device_uuid,
        "timestamp": server_time_utc,
        "latitude": punch.latitude,
        "longitude": punch.longitude,
        "is_mock_location": punch.is_mock_location,
        "biometric_verified": punch.biometric_verified,
        "punch_type": effective_punch_type,
        "tz_offset_minutes": punch.tz_offset_minutes,
        "adms_status": "pending",
        "server_sync_status": "pending",
        "client_punch_id": punch.client_punch_id,
        "gps_time_validated": punch.gps_time_validated,
        "notes": notes,
        "selfie_base64": getattr(punch, 'selfie_base64', None),
        "in_fence": in_fence,
        "distance": distance,
        "best_branch": best_branch,
    }


def create_punch_log(db: Session, data: dict) -> PunchLog:
    """Create a PunchLog record from validated data dict."""
    log = PunchLog(
        employee_id=data["employee_id"],
        device_uuid=data["device_uuid"],
        timestamp=data["timestamp"],
        latitude=data["latitude"],
        longitude=data["longitude"],
        is_mock_location=data["is_mock_location"],
        biometric_verified=data["biometric_verified"],
        punch_type=data["punch_type"],
        tz_offset_minutes=data["tz_offset_minutes"],
        adms_status=data["adms_status"],
        server_sync_status=data["server_sync_status"],
        client_punch_id=data.get("client_punch_id"),
        gps_time_validated=data.get("gps_time_validated", False),
        notes=data.get("notes"),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
