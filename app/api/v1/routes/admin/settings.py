import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database.models import ADMSTarget, AppConfig, AdminUser
from app.services.auth_ui import get_current_admin, get_password_hash
from app.services.adms_service import test_adms_connection
from app.api.v1.schemas import ADMSConfigRequest, AppConfigRequest, SmtpSettingsRequest, SmtpSettingsResponse, ProfileUpdateRequest
from app.api.v1.routes.admin.base import get_db, log_audit_action

logger = structlog.get_logger()

router = APIRouter(tags=["Admin UI - Settings"])

@router.post("/ui/settings")
async def update_settings(
    config: ADMSConfigRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    target = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
    if not target:
        target = ADMSTarget()
        db.add(target)

    from app.services.crypto import encrypt_value
    target.server_url = config.server_url
    target.serial_number = encrypt_value(config.serial_number) if config.serial_number else ""
    target.device_name = config.device_name
    target.timezone_offset = config.timezone_offset
    db.commit()
    logger.info(f"ADMS Config updated: {config.server_url} SN={config.serial_number}")
    return {"status": "success"}

@router.post("/ui/test-connection")
async def ui_test_connection(
    config: ADMSConfigRequest,
    admin: AdminUser = Depends(get_current_admin),
):
    success, message = await test_adms_connection(config.server_url, config.serial_number, config.device_name)
    return {"success": success, "message": message}

@router.get("/ui/app-settings")
async def get_app_settings(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    max_devices = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    return {"max_devices_per_employee": int(max_devices.value) if max_devices else 5}

@router.post("/ui/app-settings")
async def update_app_settings(
    config: AppConfigRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    entry = db.query(AppConfig).filter(AppConfig.key == "max_devices_per_employee").first()
    if entry:
        entry.value = str(config.max_devices_per_employee)
    else:
        db.add(AppConfig(
            key="max_devices_per_employee",
            value=str(config.max_devices_per_employee),
            description="Maximum number of devices an employee can register",
        ))
    db.commit()
    return {"status": "success"}

@router.get("/ui/app-settings/smtp", response_model=SmtpSettingsResponse)
async def get_smtp_settings(
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    def get_config_val(key, default=""):
        cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
        return cfg.value if cfg else default

    return SmtpSettingsResponse(
        smtp_host=get_config_val("smtp_host", ""),
        smtp_port=int(get_config_val("smtp_port", "587")),
        smtp_user=get_config_val("smtp_user", ""),
        smtp_password_set=bool(get_config_val("smtp_password", "")),
        hr_email_recipients=get_config_val("hr_email_recipients", "")
    )

@router.post("/ui/app-settings/smtp")
async def update_smtp_settings(
    config: SmtpSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    def set_config_val(key, val, description=None):
        entry = db.query(AppConfig).filter(AppConfig.key == key).first()
        if entry:
            entry.value = str(val)
        else:
            db.add(AppConfig(
                key=key,
                value=str(val),
                description=description
            ))

    set_config_val("smtp_host", config.smtp_host, "HR report SMTP host server")
    set_config_val("smtp_port", config.smtp_port, "HR report SMTP server port")
    set_config_val("smtp_user", config.smtp_user, "HR report SMTP account user")
    set_config_val("hr_email_recipients", config.hr_email_recipients, "HR report email recipients (comma separated)")

    if config.smtp_password and config.smtp_password != "__UNCHANGED__":
        from app.services.crypto import encrypt_value
        set_config_val("smtp_password", encrypt_value(config.smtp_password), "HR report SMTP account password")

    db.commit()

    log_audit_action(
        db=db,
        admin_username=admin.username,
        action="update_smtp_settings",
        target_type="AppConfig",
        details=f"SMTP configurations updated (host: {config.smtp_host}, user: {config.smtp_user})",
        request=request
    )

    return {"status": "success"}

@router.post("/ui/profile")
async def update_admin_profile(
    req: ProfileUpdateRequest,
    db: Session = Depends(get_db),
    admin: AdminUser = Depends(get_current_admin),
):
    if req.username != admin.username:
        raise HTTPException(status_code=403, detail="Cannot change another user's profile")
    admin.hashed_password = get_password_hash(req.new_password)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return {"status": "success"}
