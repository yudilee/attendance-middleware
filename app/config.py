"""Application configuration using Pydantic settings."""
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql://attendance:attendance123@db:5432/attendance_db"
    
    # Redis
    redis_url: str = "redis://redis:6379/0"
    
    # ADMS
    adms_server_url: Optional[str] = None
    adms_serial_number: Optional[str] = None
    adms_device_name: Optional[str] = "AttendanceMiddleware"
    
    # Security
    secret_key: Optional[str] = None
    api_key_salt: Optional[str] = None
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    encryption_key: Optional[str] = None
    
    # App
    min_app_version: str = "1.0.0"
    max_daily_punches: int = 10
    max_timestamp_deviation_seconds: int = 300
    sync_retry_interval_seconds: int = 300
    
    # CORS
    cors_origins: str = "*"
    
    # Environment
    env: str = "development"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
