from pydantic import BaseModel, ConfigDict
from datetime import datetime

class PunchRequest(BaseModel):
    employee_id: str
    device_uuid: str
    timestamp: str  # ISO 8601 in device local time
    latitude: float
    longitude: float
    is_mock_location: bool
    biometric_verified: bool
    punch_type: str  # Check In / Check Out
    tz_offset_minutes: int = 420  # Timezone offset from UTC in minutes (default: GMT+7)
    gps_time_validated: bool = False  # Whether timestamp was cross-validated with GPS

    model_config = ConfigDict(from_attributes=True)

class PunchResponse(BaseModel):
    status: str
    message: str
    server_time: datetime
    log_id: int

class DeviceConfigResponse(BaseModel):
    status: str # "assigned" or "pending"
    branch_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    radius_meters: float | None = None
