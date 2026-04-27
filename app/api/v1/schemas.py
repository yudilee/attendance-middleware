from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional


class PunchRequest(BaseModel):
    employee_id: str
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
    branch_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_meters: Optional[float] = None
    message: Optional[str] = None      # Human-readable status message


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


class AppStatusResponse(BaseModel):
    status: str
    min_version: str
    message: Optional[str] = None
