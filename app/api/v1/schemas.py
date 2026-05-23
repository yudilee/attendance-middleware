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
    distance_meters: Optional[float] = None
    branch_name: Optional[str] = None
    in_fence: Optional[bool] = None


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
    employee_name: Optional[str] = None     # Employee full name
    employee_id: Optional[str] = None       # Employee ID

    model_config = ConfigDict(from_attributes=True)


class BranchInfo(BaseModel):
    id: int
    name: str
    latitude: float
    longitude: float
    radius_meters: float
    qr_code_enabled: bool = False
    qr_code_data: Optional[str] = None
    nfc_enabled: bool = False
    nfc_tag_data: Optional[str] = None
    checkpoints: list["CheckpointInfo"] = []


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


# ═══════════════════ Admin UI Schemas ═══════════════════
# These were previously defined inline in main.py and have been
# extracted here for the route module refactoring.

class ADMSConfigRequest(BaseModel):
    server_url: str
    serial_number: str
    device_name: str
    timezone_offset: int


class AppConfigRequest(BaseModel):
    max_devices_per_employee: int = 5


class ProfileUpdateRequest(BaseModel):
    username: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: Optional[str] = None
    role: Optional[str] = "admin"


class DeviceLabelRequest(BaseModel):
    label: str
    notes: str = ""


class BranchRequest(BaseModel):
    name: str
    latitude: float
    longitude: float
    radius_meters: float
    qr_code_enabled: bool = False
    qr_code_data: Optional[str] = None
    nfc_enabled: bool = False
    nfc_tag_data: Optional[str] = None


class PunchTypePayload(BaseModel):
    code: str
    label: str
    adms_status_code: str
    icon: str = "circle"
    color_hex: str = "#000000"
    display_order: int = 0
    requires_geofence: bool = True
    is_active: bool = True


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

class OnboardGenerateRequest(BaseModel):
    employee_id: str
    branch_id: int
    api_key_id: int

class OnboardDeviceRequest(BaseModel):
    device_uuid: str
    device_label: Optional[str] = None
    token: str


# ═══════════════════ Branch Checkpoint Schemas ═══════════════════

class CheckpointInfo(BaseModel):
    """Represents a single clock-in point within a branch."""
    id: int
    branch_id: int
    name: str
    latitude: float
    longitude: float
    radius_meters: float
    is_active: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CheckpointCreate(BaseModel):
    name: str
    latitude: float
    longitude: float
    radius_meters: float = 50.0
    is_active: bool = True


class CheckpointUpdate(BaseModel):
    name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_meters: Optional[float] = None
    is_active: Optional[bool] = None


# ═══════════════════ Employee Schemas ═══════════════════

class EmployeeCreatePayload(BaseModel):
    employee_id: str
    full_name: str
    department: Optional[str] = None
    is_active: bool = True


class EmployeeUpdatePayload(BaseModel):
    full_name: Optional[str] = None
    department: Optional[str] = None
    is_active: Optional[bool] = None


class EmployeeResponse(BaseModel):
    employee_id: str
    full_name: str
    department: Optional[str] = None
    is_active: bool
    is_deleted: bool
    last_synced: Optional[datetime] = None
    device_count: int
    adms_registered: bool

    model_config = ConfigDict(from_attributes=True)
