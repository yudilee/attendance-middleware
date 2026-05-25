import re
import structlog
import httpx
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database.models import (
    Employee, DeviceBinding, ADMSRegisteredEmployee, ADMSCredential, PunchLog,
    AppConfig, ApiKey, PunchType, ADMSTarget, Branch, EmployeeSupervisor, BindingBranch, AdminUser
)
from app.services.auth_ui import get_current_admin
from app.services.adms_scraper import sync_employees_from_adms
from app.services.adms_service import get_adms_config, register_employee_on_adms, delete_employee_from_adms
from app.api.v1.schemas import (
    EmployeeResponse, EmployeeCreatePayload, EmployeeUpdatePayload, PunchTypePayload,
    SupervisorAssignment, ADMSCredentialPayload
)
from app.cache import invalidate_cache
from app.api.v1.routes.admin.base import get_db, log_audit_action, templates, get_arq_pool

logger = structlog.get_logger()

router = APIRouter(tags=["Admin UI - Employee & Sync Management"])

# ─── PUNCH TYPES ───
@router.get("/ui/punch-types")
async def get_ui_punch_types(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    types = db.query(PunchType).order_by(PunchType.display_order).all()
    return [
        {"code": t.code, "label": t.label, "adms_status_code": t.adms_status_code,
         "display_order": t.display_order, "icon": t.icon, "color_hex": t.color_hex,
         "requires_geofence": t.requires_geofence}
        for t in types
    ]

@router.post("/ui/punch-types")
async def create_punch_type(
    payload: PunchTypePayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    existing = db.query(PunchType).filter(PunchType.code == payload.code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Code already exists")
    pt = PunchType(**payload.model_dump())
    db.add(pt)
    db.commit()
    await invalidate_cache("punch_types:*")
    return {"status": "created"}

@router.put("/ui/punch-types/{code}")
async def update_punch_type(
    code: str,
    payload: PunchTypePayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    pt = db.query(PunchType).filter(PunchType.code == code).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Punch type not found")
    for key, value in payload.model_dump().items():
        setattr(pt, key, value)
    db.commit()
    await invalidate_cache("punch_types:*")
    return {"status": "updated"}

@router.delete("/ui/punch-types/{code}")
async def delete_punch_type(
    code: str,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    pt = db.query(PunchType).filter(PunchType.code == code).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Punch type not found")
    db.delete(pt)
    db.commit()
    await invalidate_cache("punch_types:*")
    return {"status": "deleted"}


# ─── ADMS SYNC CREDENTIALS & TRIGGERS ───
@router.get("/ui/adms-credentials")
async def get_adms_credentials(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    creds = db.query(ADMSCredential).filter(ADMSCredential.is_active == True).first()
    if not creds:
        return {"url": "", "username": "", "password": ""}
    return {"url": creds.url, "username": creds.username, "password": creds.password}

@router.post("/ui/adms-credentials")
async def save_adms_credentials(
    payload: ADMSCredentialPayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    creds = db.query(ADMSCredential).filter(ADMSCredential.is_active == True).first()
    if creds:
        creds.url = payload.url
        creds.username = payload.username
        creds.password = payload.password
    else:
        creds = ADMSCredential(url=payload.url, username=payload.username, password=payload.password)
        db.add(creds)
    db.commit()
    return {"status": "success", "message": "ADMS credentials saved."}

@router.post("/ui/adms-sync")
async def trigger_adms_sync(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    pool = get_arq_pool()
    if pool:
        try:
            job = await pool.enqueue_job("retry_failed_punches")
            # Continue to sync employees synchronously or return triggered response?
            # We keep matching existing logic:
            # return {"status": "triggered", "job_id": job.job_id}
            # Wait, the old code says:
            # if arq_pool: ... return {"status": "triggered", "job_id": job.job_id}
            # so we do that:
            return {"status": "triggered", "job_id": job.job_id}
        except Exception as e:
            logger.warning("adms_retry_enqueue_failed", error=str(e))
    success, msg = sync_employees_from_adms(db)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}

@router.post("/ui/adms-sync-to-server")
async def trigger_adms_sync_to_server(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Push all local employee names back to the ADMS server.
    """
    from app.services.adms_service import sync_all_employees_to_adms
    success, msg = await sync_all_employees_to_adms(db)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}

@router.get("/ui/adms-sync-info")
async def get_adms_sync_info(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    def get_val(key, default="Never"):
        cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
        return cfg.value if cfg else default
    return {
        "last_sync": get_val("last_adms_sync_time"),
        "last_count": get_val("last_adms_sync_count"),
        "last_status": get_val("last_adms_sync_status"),
        "total_employees": db.query(Employee).filter(Employee.is_deleted == False).count(),
        "heartbeat_connected": get_val("adms_connected", "false") == "true",
        "heartbeat_last_contact": get_val("adms_last_contact", ""),
        "heartbeat_last_error": get_val("adms_last_error", ""),
    }

@router.get("/ui/adms-sync-status")
async def get_adms_sync_status(
    request: Request,
    db: Session = Depends(get_db),
    current_user: AdminUser = Depends(get_current_admin),
):
    total = db.query(PunchLog).count()
    synced = db.query(PunchLog).filter(PunchLog.server_sync_status == "synced").count()
    pending = db.query(PunchLog).filter(PunchLog.server_sync_status == "pending").count()
    failed = db.query(PunchLog).filter(PunchLog.server_sync_status == "failed").count()

    recent_failures = db.query(PunchLog).filter(
        PunchLog.server_sync_status == "failed",
    ).order_by(PunchLog.timestamp.desc()).limit(50).all()

    last_24h = datetime.utcnow() - timedelta(hours=24)
    synced_24h = db.query(PunchLog).filter(
        PunchLog.server_sync_status == "synced",
        PunchLog.synced_at >= last_24h,
    ).count()

    def get_cfg(key: str, default: str = "") -> str:
        cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
        return cfg.value if cfg else default

    adms_connected = get_cfg("adms_connected", "false") == "true"
    raw_lc = get_cfg("adms_last_contact", "")
    adms_last_handshake = "Never"
    if raw_lc:
        try:
            lc_dt = datetime.fromisoformat(raw_lc)
            adms_last_handshake = lc_dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            pass
    adms_error = get_cfg("adms_last_error", "") or None
    pool = get_arq_pool()
    worker_running = pool is not None

    return templates.TemplateResponse(
        request=request,
        name="adms_sync.html",
        context={
            "request": request,
            "stats": {
                "total": total,
                "synced": synced,
                "pending": pending,
                "failed": failed,
                "synced_24h": synced_24h,
                "sync_rate": round((synced / total * 100), 1) if total > 0 else 0,
            },
            "recent_failures": recent_failures,
            "adms_connected": adms_connected,
            "adms_last_handshake": adms_last_handshake,
            "adms_error": adms_error,
            "worker_running": worker_running,
            "app_settings": {"max_devices_per_employee": 5},
        },
    )


# ─── EMPLOYEES CRUD ───
@router.get("/ui/employees/count")
async def get_employee_count(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    return {"count": db.query(Employee).filter(Employee.is_deleted == False).count()}

@router.get("/ui/employees/list")
async def list_employees(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    emps = db.query(Employee).filter(Employee.is_deleted == False).order_by(Employee.full_name).all()
    return [{"id": e.employee_id, "name": e.full_name, "dept": e.department} for e in emps]

@router.get("/ui/employees", response_model=list[EmployeeResponse])
async def list_employees_detailed(
    page: Optional[int] = None,
    per_page: Optional[int] = None,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Get detailed list of employees including device count and ADMS registration status.
    Filters out soft-deleted employees. Supports optional pagination.
    """
    query = db.query(Employee).filter(Employee.is_deleted == False).order_by(Employee.full_name)
    if page is not None and per_page is not None:
        query = query.limit(per_page).offset((page - 1) * per_page)
    emps = query.all()
    
    active_bindings = db.query(DeviceBinding.employee_id, func.count(DeviceBinding.id)).\
        filter(DeviceBinding.is_active == True).\
        group_by(DeviceBinding.employee_id).all()
    device_counts = {emp_id: count for emp_id, count in active_bindings if emp_id}

    adms_registered = db.query(ADMSRegisteredEmployee.employee_id).all()
    adms_registered_set = {r[0] for r in adms_registered if r[0]}

    result = []
    for e in emps:
        result.append(EmployeeResponse(
            employee_id=e.employee_id,
            full_name=e.full_name,
            department=e.department,
            is_active=e.is_active,
            is_deleted=e.is_deleted,
            employee_type=e.employee_type or "regular",
            company_id=e.company_id,
            group_id=e.group_id,
            shift_schedule_id=e.shift_schedule_id,
            last_synced=e.last_synced,
            device_count=device_counts.get(e.employee_id, 0),
            adms_registered=(e.employee_id in adms_registered_set)
        ))
    return result

@router.post("/ui/employees")
async def create_employee(
    payload: EmployeeCreatePayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Create a new employee locally.
    Validates unique employee_id and runs registration on ADMS in the background.
    """
    if not re.match(r"^\d+$", payload.employee_id):
        raise HTTPException(status_code=400, detail="Employee ID (PIN) must contain digits only.")
    
    existing = db.query(Employee).filter(Employee.employee_id == payload.employee_id).first()
    if existing:
        if existing.is_deleted:
            existing.is_deleted = False
            existing.full_name = payload.full_name
            existing.department = payload.department
            existing.is_active = payload.is_active
            existing.employee_type = payload.employee_type
            existing.company_id = payload.company_id
            existing.group_id = payload.group_id
            existing.shift_schedule_id = payload.shift_schedule_id
            existing.last_synced = datetime.utcnow()
            db.commit()
            db.refresh(existing)
            logger.info("employee_reactivated", employee_id=payload.employee_id, admin=admin.username)
            log_audit_action(db, admin.username, "reactivated_employee", "employee", payload.employee_id, f"Reactivated employee {payload.full_name} ({payload.employee_type})")
        else:
            raise HTTPException(status_code=400, detail=f"Employee ID {payload.employee_id} already exists.")
    else:
        new_emp = Employee(
            employee_id=payload.employee_id,
            full_name=payload.full_name,
            department=payload.department,
            is_active=payload.is_active,
            employee_type=payload.employee_type,
            company_id=payload.company_id,
            group_id=payload.group_id,
            shift_schedule_id=payload.shift_schedule_id,
            is_deleted=False
        )
        db.add(new_emp)
        db.commit()
        logger.info("employee_created", employee_id=payload.employee_id, admin=admin.username)
        log_audit_action(db, admin.username, "created_employee", "employee", payload.employee_id, f"Created employee {payload.full_name} ({payload.employee_type})")

    if payload.is_active and payload.employee_type == "regular":
        server_url, sn, _ = get_adms_config()
        if server_url:
            async def _bg_reg():
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await register_employee_on_adms(client, server_url, sn, payload.employee_id, payload.full_name)
                except Exception as e:
                    logger.warning("adms_bg_registration_failed", employee_id=payload.employee_id, error=str(e))
            
            asyncio.create_task(_bg_reg())

    return {"status": "success", "message": "Employee created successfully"}

@router.put("/ui/employees/{employee_id}")
async def update_employee(
    employee_id: str,
    payload: EmployeeUpdatePayload,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Update local employee data.
    Aligns changes with the ADMS server.
    """
    emp = db.query(Employee).filter(Employee.employee_id == employee_id, Employee.is_deleted == False).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    status_changed = False
    name_changed = False
    
    if payload.full_name is not None and payload.full_name != emp.full_name:
        emp.full_name = payload.full_name
        name_changed = True
        
    if payload.department is not None:
        emp.department = payload.department
        
    if payload.is_active is not None and payload.is_active != emp.is_active:
        emp.is_active = payload.is_active
        status_changed = True

    if payload.employee_type is not None:
        emp.employee_type = payload.employee_type
        
    if payload.company_id is not None:
        emp.company_id = payload.company_id
        
    if payload.group_id is not None:
        emp.group_id = payload.group_id
        
    if payload.shift_schedule_id is not None:
        emp.shift_schedule_id = payload.shift_schedule_id

    emp.last_synced = datetime.utcnow()
    db.commit()

    logger.info("employee_updated", employee_id=employee_id, admin=admin.username)
    log_audit_action(db, admin.username, "updated_employee", "employee", employee_id, f"Updated employee {emp.full_name} details")

    if status_changed and not emp.is_active:
        bindings = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).all()
        for binding in bindings:
            await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")

    if emp.employee_type == "regular" and (name_changed or (status_changed and emp.is_active)):
        server_url, sn, _ = get_adms_config()
        if server_url:
            async def _bg_sync():
                try:
                    # Remove local tracker so register_employee_on_adms forces a push
                    from app.database.models import SessionLocal
                    local_db = SessionLocal()
                    try:
                        existing = local_db.query(ADMSRegisteredEmployee).filter(
                            ADMSRegisteredEmployee.employee_id == employee_id
                        ).first()
                        if existing:
                            local_db.delete(existing)
                            local_db.commit()
                    finally:
                        local_db.close()

                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await register_employee_on_adms(client, server_url, sn, employee_id, emp.full_name)
                except Exception as e:
                    logger.warning("adms_bg_sync_failed", employee_id=employee_id, error=str(e))
            
            asyncio.create_task(_bg_sync())

    return {"status": "success", "message": "Employee updated successfully"}

@router.delete("/ui/employees/{employee_id}")
async def soft_delete_employee(
    employee_id: str,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    """
    Soft-deletes employee from local database (marks is_deleted = True, is_active = False).
    Cascade deletes device bindings, supervisor assignments, ADMSRegisteredEmployee records,
    and invalidates all cache. Keeps the ADMS server intact.
    """
    emp = db.query(Employee).filter(Employee.employee_id == employee_id, Employee.is_deleted == False).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    emp.is_deleted = True
    emp.is_active = False
    emp.last_synced = datetime.utcnow()

    bindings = db.query(DeviceBinding).filter(DeviceBinding.employee_id == employee_id).all()
    for binding in bindings:
        await invalidate_cache(f"device_config:{binding.api_key_id}:{binding.device_uuid}")
        db.query(BindingBranch).filter(BindingBranch.binding_id == binding.id).delete()
        db.delete(binding)

    db.query(EmployeeSupervisor).filter(
        (EmployeeSupervisor.supervisor_id == employee_id) |
        (EmployeeSupervisor.employee_id == employee_id)
    ).delete()

    adms_track = db.query(ADMSRegisteredEmployee).filter(
        ADMSRegisteredEmployee.employee_id == employee_id
    ).first()
    if adms_track:
        db.delete(adms_track)

    db.commit()
    logger.info("employee_soft_deleted", employee_id=employee_id, admin=admin.username)
    log_audit_action(db, admin.username, "deleted_employee", "employee", employee_id, f"Soft-deleted employee {emp.full_name} and cascade-revoked device bindings")
    return {"status": "success", "message": f"Employee {employee_id} soft-deleted and app access cascade-revoked"}

@router.post("/ui/employees/{employee_id}/delete-from-adms")
async def delete_employee_from_adms_endpoint(
    employee_id: str,
    current_user: AdminUser = Depends(get_current_admin),
):
    """
    Delete an employee from the ADMS server.
    Sends a delete command via ZKTeco OPERLOG.
    """
    server_url, sn, _ = get_adms_config()
    if not server_url:
        raise HTTPException(status_code=400, detail="ADMS server not configured")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        success = await delete_employee_from_adms(client, server_url, sn, employee_id)

    if success:
        logger.info(f"🗑️ Admin {current_user.username} deleted employee {employee_id} from ADMS")
        return {"status": "success", "message": f"Delete command sent for {employee_id}"}
    else:
        raise HTTPException(status_code=502, detail=f"Failed to delete {employee_id} from ADMS")


# ─── SUPERVISOR MANAGEMENT (UI) ───
@router.post("/ui/supervisors/assign")
async def assign_supervisor(
    assignment: SupervisorAssignment,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(EmployeeSupervisor).filter(
        EmployeeSupervisor.supervisor_id == assignment.supervisor_id,
        EmployeeSupervisor.employee_id == assignment.employee_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Assignment already exists")
    mapping = EmployeeSupervisor(
        supervisor_id=assignment.supervisor_id,
        employee_id=assignment.employee_id,
    )
    db.add(mapping)
    db.commit()
    return {"status": "assigned"}

@router.delete("/ui/supervisors/assign/{mapping_id}")
async def remove_supervisor(
    mapping_id: int,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    mapping = db.query(EmployeeSupervisor).filter(EmployeeSupervisor.id == mapping_id).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Assignment not found")
    db.delete(mapping)
    db.commit()
    return {"status": "removed"}

@router.get("/ui/supervisors/list")
async def list_supervisors(
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    assignments = db.query(EmployeeSupervisor).all()
    return {
        "assignments": [
            {
                "id": a.id,
                "supervisor_id": a.supervisor_id,
                "employee_id": a.employee_id,
                "created_at": a.created_at.isoformat(),
            }
            for a in assignments
        ],
    }

@router.get("/ui/supervisors")
async def supervisor_management(
    request: Request,
    current_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "section": "supervisors",
            "admin": current_user,
            "devices": db.query(DeviceBinding).all(),
            "branches": db.query(Branch).all(),
            "api_keys": db.query(ApiKey).all(),
            "punch_types": db.query(PunchType).all(),
            "adms_targets": db.query(ADMSTarget).all(),
            "app_settings": {"max_devices_per_employee": 5},
        },
    )
