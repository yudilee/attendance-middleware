from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import datetime
import os

Base = declarative_base()


class DeviceBinding(Base):
    __tablename__ = "device_bindings"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String, unique=True, index=True)
    device_uuid = Column(String, index=True)
    branch_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ADMSTarget(Base):
    __tablename__ = "adms_targets"
    id = Column(Integer, primary_key=True, index=True)
    server_url = Column(String, default="")
    serial_number = Column(String, default="")
    device_name = Column(String, default="Mobile Gateway")
    is_active = Column(Boolean, default=True)
    timezone_offset = Column(Integer, default=7)   # Default GMT+7 (WIB)
    last_contact = Column(DateTime, nullable=True)

class AdminUser(Base):
    __tablename__ = "admin_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Branch(Base):
    """Configurable branch site for geofencing."""
    __tablename__ = "branches"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, default="Default Office")
    latitude = Column(Float, default=0.0)
    longitude = Column(Float, default=0.0)
    radius_meters = Column(Float, default=100.0)
    is_active = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class ApiKey(Base):
    """API keys issued to mobile clients for authenticating punch requests."""
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True, index=True)
    key_value = Column(String, unique=True, index=True)
    label = Column(String, default="Mobile Client")       # Human-readable label
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)


class ADMSRegisteredEmployee(Base):
    """Track employees that have been auto-registered on the ADMS server."""
    __tablename__ = "adms_registered_employees"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String, unique=True, index=True)
    employee_name = Column(String, default="Mobile User")
    registered_at = Column(DateTime, default=datetime.datetime.utcnow)


class PunchLog(Base):
    __tablename__ = "punch_logs"
    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(String, index=True)
    device_uuid = Column(String)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    latitude = Column(Float)
    longitude = Column(Float)
    is_mock_location = Column(Boolean)
    biometric_verified = Column(Boolean)
    punch_type = Column(String)                   # "In" or "Out"
    tz_offset_minutes = Column(Integer, default=420)  # Offset from UTC in minutes
    adms_status = Column(String, default="pending")  # pending / uploaded / failed


# Ensure the data directory exists for SQLite
if not os.path.exists("./data"):
    os.makedirs("./data")

SQLALCHEMY_DATABASE_URL = "sqlite:///./data/attendance.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)

    # ── Auto-Migration for SQLite ───────────────────────────────────────────
    # SQLAlchemy create_all() does not add new columns to existing tables.
    # We manually add them here via raw SQL for production self-healing.
    from sqlalchemy import text
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE adms_targets ADD COLUMN timezone_offset INTEGER DEFAULT 7;"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE punch_logs ADD COLUMN tz_offset_minutes INTEGER DEFAULT 420;"))
            conn.commit()
        except Exception:
            pass

    db = SessionLocal()
    try:
        # Seed default Branch
        if db.query(Branch).count() == 0:
            db.add(Branch())
            db.commit()

        # Seed default ADMS target
        if db.query(ADMSTarget).count() == 0:
            db.add(ADMSTarget())
            db.commit()
    finally:
        db.close()
