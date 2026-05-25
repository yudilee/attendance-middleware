from pydantic import BaseModel, ConfigDict
from datetime import datetime, date
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
    signature: Optional[str] = None      # HMAC signature of request payload

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
    geofence_type: str = "circle"
    polygon_coordinates: Optional[str] = None
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


class SmtpSettingsRequest(BaseModel):
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: Optional[str] = None
    hr_email_recipients: str


class SmtpSettingsResponse(BaseModel):
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password_set: bool
    hr_email_recipients: str



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
    geofence_type: Optional[str] = "circle"
    polygon_coordinates: Optional[str] = None
    qr_code_enabled: bool = False
    qr_code_data: Optional[str] = None
    nfc_enabled: bool = False
    nfc_tag_data: Optional[str] = None
    company_id: Optional[int] = None
    shift_schedule_id: Optional[int] = None
    timezone_offset: Optional[int] = 7
    timezone_name: Optional[str] = "Asia/Jakarta"


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
    geofence_type: str = "circle"
    polygon_coordinates: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CheckpointCreate(BaseModel):
    name: str
    latitude: float
    longitude: float
    radius_meters: float = 50.0
    is_active: bool = True
    geofence_type: str = "circle"
    polygon_coordinates: Optional[str] = None


class CheckpointUpdate(BaseModel):
    name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_meters: Optional[float] = None
    is_active: Optional[bool] = None
    geofence_type: Optional[str] = None
    polygon_coordinates: Optional[str] = None


# ═══════════════════ Employee Schemas ═══════════════════

class EmployeeCreatePayload(BaseModel):
    employee_id: str
    full_name: str
    department: Optional[str] = None
    is_active: bool = True
    employee_type: str = "regular"  # "regular", "internship", "daily_worker"
    company_id: Optional[int] = None
    group_id: Optional[int] = None
    shift_schedule_id: Optional[int] = None


class EmployeeUpdatePayload(BaseModel):
    full_name: Optional[str] = None
    department: Optional[str] = None
    is_active: Optional[bool] = None
    employee_type: Optional[str] = None
    company_id: Optional[int] = None
    group_id: Optional[int] = None
    shift_schedule_id: Optional[int] = None


class EmployeeResponse(BaseModel):
    employee_id: str
    full_name: str
    department: Optional[str] = None
    is_active: bool
    is_deleted: bool
    employee_type: str
    company_id: Optional[int] = None
    group_id: Optional[int] = None
    shift_schedule_id: Optional[int] = None
    last_synced: Optional[datetime] = None
    device_count: int
    adms_registered: bool

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════ Shift Schedule Schemas ═══════════════════
class ShiftScheduleCreate(BaseModel):
    name: str
    start_time: str = "08:00"
    end_time: str = "17:00"
    grace_minutes: int = 15
    min_work_hours: float = 8.0
    overtime_after_hours: float = 9.0
    working_days: str = "1,2,3,4,5"
    is_default: bool = False
    schedule_type: Optional[str] = "weekly"
    interval_days: Optional[int] = None
    anchor_date: Optional[date] = None
    # Tiered Overtime Policy Engine (Phase 5B)
    overtime_multiplier_1: float = 1.5
    overtime_multiplier_2: float = 2.0
    overtime_threshold_2_hours: Optional[float] = None
    weekend_overtime_multiplier: float = 2.0
    holiday_overtime_multiplier: float = 3.0
    monthly_overtime_cap_hours: Optional[float] = None
    # Auto Clock-Out Configs (Phase 5C)
    auto_clockout_enabled: bool = False
    auto_clockout_buffer_minutes: int = 60


class ShiftScheduleUpdate(BaseModel):
    name: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    grace_minutes: Optional[int] = None
    min_work_hours: Optional[float] = None
    overtime_after_hours: Optional[float] = None
    working_days: Optional[str] = None
    is_default: Optional[bool] = None
    schedule_type: Optional[str] = None
    interval_days: Optional[int] = None
    anchor_date: Optional[date] = None
    # Tiered Overtime Policy Engine (Phase 5B)
    overtime_multiplier_1: Optional[float] = None
    overtime_multiplier_2: Optional[float] = None
    overtime_threshold_2_hours: Optional[float] = None
    weekend_overtime_multiplier: Optional[float] = None
    holiday_overtime_multiplier: Optional[float] = None
    monthly_overtime_cap_hours: Optional[float] = None
    # Auto Clock-Out Configs (Phase 5C)
    auto_clockout_enabled: Optional[bool] = None
    auto_clockout_buffer_minutes: Optional[int] = None


class ShiftScheduleResponse(BaseModel):
    id: int
    name: str
    start_time: str
    end_time: str
    grace_minutes: int
    min_work_hours: float
    overtime_after_hours: float
    working_days: str
    is_default: bool
    schedule_type: Optional[str]
    interval_days: Optional[int]
    anchor_date: Optional[date]
    created_at: datetime
    # Tiered Overtime Policy Engine (Phase 5B)
    overtime_multiplier_1: float
    overtime_multiplier_2: float
    overtime_threshold_2_hours: Optional[float]
    weekend_overtime_multiplier: float
    holiday_overtime_multiplier: float
    monthly_overtime_cap_hours: Optional[float]
    # Auto Clock-Out Configs (Phase 5C)
    auto_clockout_enabled: bool
    auto_clockout_buffer_minutes: int

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════ Company Schemas ═══════════════════
class CompanyCreate(BaseModel):
    name: str
    code: str
    is_active: bool = True
    shift_schedule_id: Optional[int] = None


class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    is_active: Optional[bool] = None
    shift_schedule_id: Optional[int] = None


class CompanyResponse(BaseModel):
    id: int
    name: str
    code: str
    is_active: bool
    shift_schedule_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════ Employee Group Schemas ═══════════════════
class EmployeeGroupCreate(BaseModel):
    name: str
    branch_id: int
    shift_schedule_id: Optional[int] = None


class EmployeeGroupUpdate(BaseModel):
    name: Optional[str] = None
    branch_id: Optional[int] = None
    shift_schedule_id: Optional[int] = None


class EmployeeGroupResponse(BaseModel):
    id: int
    name: str
    branch_id: int
    shift_schedule_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════ Holiday Schemas ═══════════════════
class HolidayCreate(BaseModel):
    name: str
    date: date
    is_recurring: bool = False


class HolidayResponse(BaseModel):
    id: int
    name: str
    date: date
    is_recurring: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════ Leave Request Schemas ═══════════════════
class LeaveRequestCreate(BaseModel):
    employee_id: str
    leave_type: str
    start_date: date
    end_date: date
    reason: Optional[str] = None


class LeaveRequestUpdate(BaseModel):
    status: str  # 'approved' or 'rejected'
    reason: Optional[str] = None


class LeaveRequestResponse(BaseModel):
    id: int
    employee_id: str
    leave_type: str
    start_date: date
    end_date: date
    reason: Optional[str] = None
    status: str
    approved_by: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════ Audit Log Schemas ═══════════════════
class AuditLogResponse(BaseModel):
    id: int
    admin_username: str
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    details: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════ Pagination Schemas ═══════════════════
class PaginationMeta(BaseModel):
    page: int
    per_page: int
    total: int
    total_pages: int

class PaginatedResponse(BaseModel):
    data: list
    pagination: PaginationMeta


# ═══════════════════ Roster Schedule Assignment Schemas (Phase 5D) ═══════════════════
class ScheduleAssignmentCreate(BaseModel):
    employee_id: str
    shift_schedule_id: int
    effective_date: date
    end_date: Optional[date] = None

class ScheduleAssignmentResponse(BaseModel):
    id: int
    employee_id: str
    shift_schedule_id: int
    effective_date: date
    end_date: Optional[date] = None
    created_by: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
