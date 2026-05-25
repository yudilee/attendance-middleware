"""ARQ background worker for ADMS sync and scheduled tasks."""
import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional

import arq
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://attendance:attendance123@db:5432/attendance_db")
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        DATABASE_URL,
        pool_size=3,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_timeout=20,
    )
SessionLocal = sessionmaker(bind=engine)

async def sync_punches_to_adms(ctx, punch_log_id: int) -> dict:
    """Push a single punch log to ADMS server. Retries with exponential backoff and dead-letter auditing."""
    from app.services.adms_service import push_to_adms as do_push
    db = SessionLocal()
    try:
        from app.database.models import PunchLog
        punch = db.query(PunchLog).filter(PunchLog.id == punch_log_id).first()
        if not punch:
            return {"status": "skipped", "reason": "punch_not_found"}

        result = await do_push(punch.id, punch.employee_id, punch.timestamp, punch.punch_type, punch.tz_offset_minutes)
        if result:
            punch.server_sync_status = "synced"
            punch.synced_at = datetime.utcnow()
            db.commit()
            return {"status": "synced", "punch_id": punch_log_id}
        else:
            punch.server_sync_status = "failed"
            punch.sync_error = "ADMS push returned failure"
            punch.sync_retry_count = (punch.sync_retry_count or 0) + 1
            if punch.sync_retry_count >= 5:
                from app.database.models import AuditLog
                log_entry = AuditLog(
                    admin_username="arq_worker",
                    action="job_dead_letter",
                    target_type="PunchLog",
                    target_id=str(punch_log_id),
                    details="Permanent failure pushing punch to ADMS. Retries exhausted (5/5). Error: ADMS push returned failure",
                )
                db.add(log_entry)
            db.commit()
            return {"status": "failed", "punch_id": punch_log_id, "error": "ADMS push returned failure"}
    except Exception as e:
        from app.database.models import PunchLog
        punch = db.query(PunchLog).filter(PunchLog.id == punch_log_id).first()
        if punch:
            punch.server_sync_status = "failed"
            punch.sync_error = str(e)
            punch.sync_retry_count = (punch.sync_retry_count or 0) + 1
            if punch.sync_retry_count >= 5:
                from app.database.models import AuditLog
                log_entry = AuditLog(
                    admin_username="arq_worker",
                    action="job_dead_letter",
                    target_type="PunchLog",
                    target_id=str(punch_log_id),
                    details=f"Permanent failure pushing punch to ADMS. Retries exhausted (5/5). Error: {e}",
                )
                db.add(log_entry)
            db.commit()
        return {"status": "error", "punch_id": punch_log_id, "error": str(e)}
    finally:
        db.close()

async def retry_failed_punches(ctx):
    """Scheduled task: retry punches with server_sync_status='failed'."""
    db = SessionLocal()
    try:
        from app.database.models import PunchLog
        failed = db.query(PunchLog).filter(
            PunchLog.server_sync_status == "failed"
        ).limit(50).all()

        results = []
        for punch in failed:
            job = await ctx["pool"].enqueue_job("sync_punches_to_adms", punch.id)
            results.append({"punch_id": punch.id, "job_id": job.job_id})

        return {"retried": len(results), "results": results}
    finally:
        db.close()

async def adms_heartbeat(ctx):
    """Scheduled task: maintain ADMS heartbeat connection.
    
    Runs every minute via ARQ cron. Writes connection state to AppConfig
    in PostgreSQL so the web dashboard (which runs in a separate process)
    can display "Connected" / "Disconnected" correctly.
    """
    from app.services.adms_service import test_adms_connection, get_adms_config, _handshake_state
    from app.database.models import AppConfig
    from datetime import datetime
    from app.database.models import SessionLocal
    try:
        server_url, sn, device_name = get_adms_config()
        if not server_url:
            _handshake_state["handshake_done"] = False
            _handshake_state["last_error"] = "No ADMS server configured"
            return {"status": "skipped", "reason": "no_server_configured"}
        
        success, message = await test_adms_connection(server_url, sn, device_name)
        
        # ── Persist heartbeat state to DB (cross-process visibility) ──
        now_iso = datetime.utcnow().isoformat()
        db = SessionLocal()
        try:
            def upsert_config(key: str, value: str):
                existing = db.query(AppConfig).filter(AppConfig.key == key).first()
                if existing:
                    existing.value = value
                else:
                    db.add(AppConfig(key=key, value=value))
            
            if success:
                upsert_config("adms_connected", "true")
                upsert_config("adms_last_contact", now_iso)
                upsert_config("adms_last_error", "")
                _handshake_state["handshake_done"] = True
                _handshake_state["last_contact"] = datetime.utcnow()
                _handshake_state["last_error"] = None
            else:
                upsert_config("adms_connected", "false")
                upsert_config("adms_last_contact", now_iso)
                upsert_config("adms_last_error", message)
                _handshake_state["handshake_done"] = False
                _handshake_state["last_error"] = message
            
            db.commit()
        finally:
            db.close()
        
        return {"status": "ok" if success else "failed", "message": message}
    except Exception as e:
        _handshake_state["handshake_done"] = False
        _handshake_state["last_error"] = str(e)
        return {"status": "error", "error": str(e)}

async def cleanup_stale_jobs(ctx):
    """Scheduled task: mark jobs older than 7 days as stale."""
    db = SessionLocal()
    try:
        from app.database.models import PunchLog
        cutoff = datetime.utcnow() - timedelta(days=7)
        stale = db.query(PunchLog).filter(
            PunchLog.server_sync_status == "pending",
            PunchLog.timestamp < cutoff
        ).update({"server_sync_status": "stale"})
        db.commit()
        return {"marked_stale": stale}
    finally:
        db.close()

async def send_clock_in_reminders(ctx):
    """Scheduled task: send clock-in reminders to all devices with FCM tokens."""
    from app.database.models import DeviceBinding
    from app.services.notification_service import send_clock_in_reminder
    db = SessionLocal()
    try:
        devices = db.query(DeviceBinding).filter(
            DeviceBinding.fcm_token.isnot(None),
            DeviceBinding.fcm_token != "",
            DeviceBinding.is_active == True,
        ).all()

        sent_count = 0
        for device in devices:
            success = send_clock_in_reminder(device.fcm_token)
            if success:
                sent_count += 1

        return {"sent": sent_count, "total": len(devices)}
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()

async def cleanup_stale_selfies(ctx):
    """Scheduled task: delete selfies older than 30 days to free disk space."""
    from app.database.models import PunchLog
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        old_punches = db.query(PunchLog).filter(
            PunchLog.timestamp < cutoff,
            PunchLog.selfie_filename.isnot(None)
        ).all()
        
        deleted_count = 0
        base_dir = os.path.dirname(os.path.abspath(__file__))
        upload_dir = os.path.join(base_dir, "..", "uploads", "selfies")
        
        for punch in old_punches:
            if punch.selfie_filename:
                filepath = os.path.join(upload_dir, punch.selfie_filename)
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        deleted_count += 1
                    except OSError:
                        pass
            punch.selfie_filename = None
            
        db.commit()
        return {"deleted_selfies": deleted_count}
    finally:
        db.close()


async def nightly_missing_punch_scan(ctx):
    """
    Nightly scan running at 23:30.
    1. Resolves employee shifts for today.
    2. Identifies missing punches (only one punch or no punches on a working day).
    3. Auto-creates a pending AttendanceCorrection entry in a single batch commit if a check-in is unpaired.
    4. Dispatches an FCM notification.
    """
    from datetime import date
    from app.database.models import Employee, PunchLog, AttendanceCorrection, DeviceBinding
    from app.services.report_service import resolve_employee_shift, pair_employee_punches
    from app.services.notification_service import send_push_notification
    
    db = SessionLocal()
    try:
        today = date.today()
        # Get all active, regular employees
        employees = db.query(Employee).filter(Employee.is_deleted == False, Employee.is_active == True).all()
        
        flagged_count = 0
        corrections_to_add = []
        notifications_to_send = []

        for emp in employees:
            # Pair punches for today
            paired = pair_employee_punches(db, emp, today, today)
            day_records = paired.get("daily_records", [])
            if not day_records:
                continue
                
            record = day_records[0]
            
            # Check if this has an unpaired check-in
            if record.get("first_in") and not record.get("last_out"):
                shift = resolve_employee_shift(db, emp, today)
                if getattr(shift, "auto_clockout_enabled", False):
                    # Auto clock-out is enabled! Generate a clock-out punch instead of raising correction
                    try:
                        sh_hour, sh_min = map(int, shift.end_time.split(":"))
                        # Fetch today's punches to resolve tz offset
                        db_start = datetime.combine(today - timedelta(days=1), datetime.min.time())
                        db_end = datetime.combine(today + timedelta(days=1), datetime.max.time())
                        day_punches = db.query(PunchLog).filter(
                            PunchLog.employee_id == emp.employee_id,
                            PunchLog.timestamp >= db_start,
                            PunchLog.timestamp <= db_end
                        ).all()
                        
                        in_punches = [p for p in day_punches if p.punch_type.lower() in ["in", "check in"]]
                        first_in_log = in_punches[0] if in_punches else None
                        tz_offset = 420
                        if first_in_log and first_in_log.tz_offset_minutes is not None:
                            tz_offset = first_in_log.tz_offset_minutes
                            
                        local_end_dt = datetime.combine(today, datetime.time(sh_hour, sh_min))
                        utc_timestamp = local_end_dt - timedelta(minutes=tz_offset)
                        
                        auto_punch = PunchLog(
                            employee_id=emp.employee_id,
                            device_uuid="auto_clockout_system",
                            timestamp=utc_timestamp,
                            latitude=0.0,
                            longitude=0.0,
                            is_mock_location=False,
                            biometric_verified=False,
                            punch_type="Out",
                            tz_offset_minutes=tz_offset,
                            adms_status="local_only",
                            is_auto_generated=True,
                            notes=f"Auto-generated clock-out punch at shift end ({shift.end_time})."
                        )
                        db.add(auto_punch)
                    except Exception:
                        # Fallback to manual correction if error
                        correction = AttendanceCorrection(
                            employee_id=emp.employee_id,
                            correction_type="missing_punch",
                            description=f"Auto-flagged missing check-out punch on {today}.",
                            status="pending"
                        )
                        corrections_to_add.append(correction)
                        flagged_count += 1
                else:
                    # Missing punch! Check if we already created a correction request for this date
                    existing = db.query(AttendanceCorrection).filter(
                        AttendanceCorrection.employee_id == emp.employee_id,
                        AttendanceCorrection.correction_type == "missing_punch",
                        AttendanceCorrection.created_at >= datetime.combine(today, datetime.time.min)
                    ).first()
                    
                    if not existing:
                        # Auto-create correction and queue for bulk insert
                        correction = AttendanceCorrection(
                            employee_id=emp.employee_id,
                            correction_type="missing_punch",
                            description=f"Auto-flagged missing check-out punch on {today}.",
                            status="pending"
                        )
                        corrections_to_add.append(correction)
                        flagged_count += 1
                        
                        # Store information to trigger notification sending later
                        devices = db.query(DeviceBinding).filter(
                            DeviceBinding.employee_id == emp.employee_id,
                            DeviceBinding.is_active == True,
                            DeviceBinding.fcm_token.isnot(None),
                            DeviceBinding.fcm_token != ""
                        ).all()
                        for dev in devices:
                            notifications_to_send.append((dev.fcm_token, emp.full_name))

        if corrections_to_add:
            db.add_all(corrections_to_add)
            db.commit()

        # Fire FCM push notifications to employee's devices
        for fcm_token, full_name in notifications_to_send:
            try:
                send_push_notification(
                    fcm_token=fcm_token,
                    title="⏰ Missing Clock-Out Detected",
                    body=f"Hi {full_name}, we noticed you missed clocking out today ({today}). Please file a correction in the Employee Portal.",
                    data={"type": "missing_punch_alert"}
                )
            except Exception:
                pass
                            
        return {"status": "success", "flagged_missing_punches": flagged_count}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


async def email_scheduled_reports(ctx):
    """
    Weekly/Monthly HR Email Excel Report generator.
    Runs via CRON, generates Openpyxl workbook, sends to HR SMTP recipients.
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    import io
    from datetime import date, timedelta
    from app.services.report_service import generate_excel_report
    
    db = SessionLocal()
    try:
        from app.database.models import AppConfig

        def get_config_val(key, default=""):
            cfg = db.query(AppConfig).filter(AppConfig.key == key).first()
            return cfg.value if cfg else default

        SMTP_HOST = get_config_val("smtp_host", os.getenv("SMTP_HOST", ""))
        SMTP_PORT_STR = get_config_val("smtp_port", "")
        SMTP_PORT = int(SMTP_PORT_STR) if SMTP_PORT_STR else int(os.getenv("SMTP_PORT", "587"))
        SMTP_USER = get_config_val("smtp_user", os.getenv("SMTP_USER", ""))
        from app.services.crypto import decrypt_value
        SMTP_PASSWORD = decrypt_value(get_config_val("smtp_password", os.getenv("SMTP_PASSWORD", "")))
        HR_RECIPIENTS = get_config_val("hr_email_recipients", os.getenv("HR_EMAIL_RECIPIENTS", ""))

        if not SMTP_HOST or not HR_RECIPIENTS:
            return {"status": "skipped", "reason": "SMTP host or recipients not configured"}

        # Let's generate report for the last 7 days
        today = date.today()
        start_d = today - timedelta(days=7)
        end_d = today
        
        # Generate Excel Report using Openpyxl
        wb = generate_excel_report(db, start_d, end_d)
        
        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        
        # Prepare email
        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = HR_RECIPIENTS
        msg['Subject'] = f"Weekly Attendance System Report ({start_d} to {end_d})"
        
        body = f"""
        Dear HR Team,
        
        Please find attached the Weekly Attendance Aggregate Excel Report for all active companies, branches, and employees.
        
        Summary Period: {start_d} to {end_d}
        Generated on: {today}
        
        Best regards,
        Virtual Attendance Middleware Automated System
        """
        msg.attach(MIMEText(body, 'plain'))
        
        # Attachment
        part = MIMEBase('application', "octet-stream")
        part.set_payload(buffer.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="attendance_report_{start_d}_to_{end_d}.xlsx"')
        msg.attach(part)
        
        # Send via SMTP
        smtp_class = smtplib.SMTP_SSL if SMTP_PORT == 465 else smtplib.SMTP
        with smtp_class(SMTP_HOST, SMTP_PORT) as server:
            if SMTP_PORT != 465:
                server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            recipients = [r.strip() for r in HR_RECIPIENTS.split(",") if r.strip()]
            server.sendmail(SMTP_USER, recipients, msg.as_string())
            
        return {"status": "success", "recipients": recipients}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


async def deliver_webhook(ctx, webhook_id: int, event: str, payload: dict):
    """Deliver webhook payload asynchronously to endpoint with HMAC signing (Phase 5F)."""
    import httpx
    import hmac
    import hashlib
    import json
    from datetime import datetime
    from app.database.models import SessionLocal, Webhook, WebhookDelivery

    db = SessionLocal()
    try:
        webhook = db.query(Webhook).filter(Webhook.id == webhook_id, Webhook.is_active == True).first()
        if not webhook:
            return {"status": "skipped", "reason": "webhook_not_found_or_inactive"}

        body = {
            "event": event,
            "timestamp": datetime.utcnow().isoformat(),
            "data": payload
        }
        body_str = json.dumps(body)
        body_bytes = body_str.encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Attendance-Webhook-Dispatcher/1.0"
        }

        if webhook.secret:
            signature = hmac.new(webhook.secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
            headers["X-Webhook-Signature"] = signature

        # Perform POST request
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.post(webhook.url, content=body_bytes, headers=headers)
                status_code = response.status_code
                error_msg = None if 200 <= status_code < 300 else f"HTTP {status_code}: {response.text[:200]}"
            except Exception as e:
                status_code = None
                error_msg = str(e)

        # Log delivery attempt
        delivery = WebhookDelivery(
            webhook_id=webhook_id,
            event=event,
            payload=body_str,
            response_status=status_code,
            delivered_at=datetime.utcnow() if error_msg is None else None,
            error=error_msg
        )
        db.add(delivery)
        db.commit()

        if error_msg:
            # Raise exception to trigger ARQ worker retry mechanism
            raise RuntimeError(f"Webhook delivery failed: {error_msg}")

        return {"status": "success", "status_code": status_code}

    finally:
        db.close()


async def startup(ctx):
    """ARQ worker startup hook — verify DB and Redis connectivity."""
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        print("worker_startup_db_ok: Backing database connectivity is verified.")
    except Exception as e:
        print(f"worker_startup_db_failed: Backing database connectivity verified error: {e}")
    finally:
        db.close()


# Worker settings
class WorkerSettings:
    on_startup = startup
    functions = [
        sync_punches_to_adms,
        retry_failed_punches,
        adms_heartbeat,
        cleanup_stale_jobs,
        send_clock_in_reminders,
        cleanup_stale_selfies,
        nightly_missing_punch_scan,
        email_scheduled_reports,
        deliver_webhook
    ]
    redis_settings = arq.connections.RedisSettings(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        database=int(os.getenv("REDIS_DB", "0"))
    )
    max_tries = 5
    max_delay = 3600  # Max retry delay: 1 hour
    min_delay = 10    # Min retry delay: 10 seconds
    backoff_coefficient = 2.0  # Exponential backoff: 10s, 20s, 40s, 80s, 160s...
    job_timeout = 30  # 30 second timeout per job
    keep_result = 3600  # Keep results for 1 hour

    # Scheduled tasks
    cron_jobs = [
        # Retry failed punches every 5 minutes
        arq.cron(retry_failed_punches, minute=5, run_at_startup=True),
        # Heartbeat every minute (run immediately on startup)
        arq.cron(adms_heartbeat, minute=set(range(60)), run_at_startup=True),
        # Cleanup stale jobs daily at midnight
        arq.cron(cleanup_stale_jobs, hour=0, minute=0),
        # Clock-in reminders every weekday at 08:00 (mon=0, tues=1, wed=2, thurs=3, fri=4)
        arq.cron(send_clock_in_reminders, hour=8, minute=0, weekday={0, 1, 2, 3, 4}),
        # Cleanup stale selfies daily at 2 AM
        arq.cron(cleanup_stale_selfies, hour=2, minute=0),
        # Missing punch scan nightly at 23:30
        arq.cron(nightly_missing_punch_scan, hour=23, minute=30),
        # Email reports every Monday at 01:00 AM
        arq.cron(email_scheduled_reports, hour=1, minute=0, weekday=0),
    ]
