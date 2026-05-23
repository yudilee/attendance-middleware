import os
import sys

# Add backend to python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database.models import Branch, BranchCheckpoint, DeviceBinding, BindingBranch, Employee

# Try localhost since we are running on host machine outside docker container
DATABASE_URL = "postgresql://attendance:attendance123@localhost:5432/attendance_db"
try:
    print(f"Attempting to connect to: {DATABASE_URL}")
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    # test query
    db.query(Employee).first()
    print("Successfully connected using localhost!")
except Exception as e:
    print(f"Failed to connect to localhost: {e}")
    # Try sqlite if it's there
    sqlite_path = "sqlite:///./data/attendance.db"
    print(f"Attempting to connect to: {sqlite_path}")
    engine = create_engine(sqlite_path)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()

print("=== EMPLOYEES ===")
employees = db.query(Employee).all()
for emp in employees:
    print(f"ID: {emp.employee_id}, Name: {emp.full_name}")

print("\n=== DEVICE BINDINGS ===")
bindings = db.query(DeviceBinding).all()
for b in bindings:
    print(f"ID: {b.id}, UUID: {b.device_uuid}, Emp: {b.employee_id}, Active: {b.is_active}, Status: {b.registration_status}")

print("\n=== BINDING BRANCH ASSIGNMENTS ===")
assignments = db.query(BindingBranch).all()
for a in assignments:
    print(f"Binding ID: {a.binding_id}, Branch ID: {a.branch_id}")

print("\n=== BRANCHES ===")
branches = db.query(Branch).all()
for b in branches:
    print(f"ID: {b.id}, Name: {b.name}, Coords: ({b.latitude}, {b.longitude}), Radius: {b.radius_meters}, Active: {b.is_active}")

print("\n=== CHECKPOINTS (MULTIPOINT) ===")
cps = db.query(BranchCheckpoint).all()
for cp in cps:
    print(f"ID: {cp.id}, Branch ID: {cp.branch_id}, Name: {cp.name}, Coords: ({cp.latitude}, {cp.longitude}), Radius: {cp.radius_meters}, Active: {cp.is_active}")

db.close()
