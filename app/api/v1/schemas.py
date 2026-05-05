from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional


class PunchRequest(BaseModel):
    employee_id: Optional[str] = None
    device_uuid: str
    timestamp: str                      # ISO 8601 in device local time
    latitude: float
    longitude: float
    is_mock_location: bool
    biometric_verified: bool
    punch_type: str                     # Must match an active PunchType.code
    tz_offset_minutes: int = 420        # Timezone offset from UTC (default: GMT+7)
    gps_time_validated: bool = False    # Whether timestamp was cross-validated with GPS
    client_punch_id: Optional[str] = None  # UUID for idempotency (from mobile)
    selfie_base64: Optional[str] = None  # Base64-encoded selfie image

    model_config = ConfigDict(from_attributes=True)


class PunchResponse(BaseModel):
    status: str
    message: str
    server_time: datetime
    log_id: int


class BatchPunchRequest(BaseModel):
    punches: list[PunchRequest]


class BatchPunchResult(BaseModel):
    client_punch_id: Optional[str]
    status: str             # "success" | "duplicate" | "error"
    log_id: Optional[int] = None
    error: Optional[str] = None


class BatchPunchResponse(BaseModel):
    synced: int
    failed: int
    results: list[BatchPunchResult]


class DeviceConfigResponse(BaseModel):
    status: str             # "pending_approval" | "pending_branch" | "active" | "suspended"
    branches: list["BranchInfo"] = []       # All branches assigned to this device
    message: Optional[str] = None           # Human-readable status message
    device_count: int = 1                   # How many devices registered for this employee
    max_devices: int = 5                    # System-wide max

    model_config = ConfigDict(from_attributes=True)


class BranchInfo(BaseModel):
    id: int
    name: str
    latitude: float
    longitude: float
    radius_meters: float
    qr_code_enabled: bool = False
    qr_code_data: Optional[str] = None


class PunchTypeResponse(BaseModel):
    code: str
    label: str
    adms_status_code: str
    display_order: int
    icon: Optional[str] = None
    color_hex: Optional[str] = None
    requires_geofence: bool


class ADMSCredentialPayload(BaseModel):
    url: str
    username: str
    password: str


from typing import Optional, List

class AppStatusResponse(BaseModel):
    status: str
    min_version: str
    message: Optional[str] = None


# ═══════════════════ Supervisor / Manager Schemas ═══════════════════

class TeamAttendanceResponse(BaseModel):
    employee_id: str
    name: str
    today_punched: bool
    first_punch_time: Optional[str] = None
    last_punch_time: Optional[str] = None
    total_hours_today: Optional[float] = None
    is_late: bool = False


class CorrectionRequest(BaseModel):
    employee_id: str
    original_punch_id: Optional[int] = None
    correction_type: str
    description: str
    proposed_timestamp: Optional[str] = None
    proposed_punch_type: Optional[str] = None


class CorrectionReview(BaseModel):
    status: str  # 'approved' or 'rejected'
    notes: Optional[str] = None


class SupervisorAssignment(BaseModel):
    supervisor_id: str
    employee_id: str
