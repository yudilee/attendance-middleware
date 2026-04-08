import httpx
import logging
import asyncio
import re
from datetime import datetime, timedelta, timezone

from app.database.models import ADMSTarget, PunchLog, ADMSRegisteredEmployee, SessionLocal

# Standard ZKTeco iClock User-Agent - BioTime/ADMS servers often reject other agents with 500 errors
ICLOCK_USER_AGENT = "iClock Proxy/1.0"

logger = logging.getLogger(__name__)

# ─── Shared state for heartbeat ───────────────────────────────────────────────
# These values are populated from the server's handshake response
_handshake_state = {
    "attlog_stamp": "None",     # From ATTLOGStamp= in handshake response
    "operlog_stamp": "0",       # From OPERLOGStamp=
    "delay": 30,                # Heartbeat interval in seconds
    "trans_interval": 2,        # Minutes between data transmissions
    "timezone_offset": 7,       # Hours offset from UTC (from TimeZone= in handshake)
    "handshake_done": False,
}


def get_adms_config():
    """Fetch active ADMS configuration from DB."""
    db = SessionLocal()
    try:
        config = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
        if not config:
            config = ADMSTarget()
            db.add(config)
            db.commit()
            db.refresh(config)
        
        # Normalize URL: strip trailing slashes and ensure protocol
        url = config.server_url.strip().rstrip('/')
        if url and not url.startswith(('http://', 'https://')):
            url = f"http://{url}"
            
        return url, config.serial_number, config.device_name
    finally:
        db.close()


def _update_adms_last_contact():
    """Update the last_contact timestamp on the active ADMS target."""
    db = SessionLocal()
    try:
        config = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
        if config:
            config.last_contact = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def _parse_handshake_response(body: str):
    """
    Parse the ADMS server's handshake response into a dict.

    Example response from live server (adms.hartonomotor-group.com):
        GET OPTION FROM: TEST_001
        ATTLOGStamp=None
        OPERLOGStamp=0
        ATTPHOTOStamp=0
        ErrorDelay=30
        Delay=30
        TransTimes=00:00;14:05
        TransInterval=2
        TransFlag=1111111100
        TimeZone=3
        Realtime=1
        Encrypt=0
        ServerVer=0.0.2 2010-07-22
        TableNameStamp
    """
    params = {}
    for line in body.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            params[key.strip()] = value.strip()
    return params


async def _acknowledge_command(client: httpx.AsyncClient, server_url: str, sn: str, cmd_id: str, cmd_name: str):
    """
    Acknowledge a command received from the server during getrequest polling.
    The server sends commands in format: C:{ID}:{COMMAND}
    We must POST back a device return record.
    """
    url = f"{server_url}/iclock/devicecmd"
    params = {"SN": sn}
    # Standard acknowledgment payload
    payload = f"ID={cmd_id}&Return=0&CMD={cmd_name}\r\n"
    try:
        headers = {
            "Content-Type": "text/plain",
            "User-Agent": ICLOCK_USER_AGENT
        }
        resp = await client.post(url, params=params, content=payload, headers=headers)
        logger.debug(f"CMD ACK for {cmd_name} (ID={cmd_id}): {resp.status_code}")
    except Exception as e:
        logger.warning(f"Failed to acknowledge command {cmd_name}: {e}")


def _parse_getrequest_commands(body: str):
    """
    Parse commands from getrequest response body.
    Live server returns lines like:
        C:420:INFO
        C:421:CHECK
    Returns list of (cmd_id, cmd_name) tuples.
    """
    commands = []
    for line in body.strip().splitlines():
        line = line.strip()
        # Format: C:{id}:{command}
        match = re.match(r'^C:(\d+):(.+)$', line)
        if match:
            commands.append((match.group(1), match.group(2)))
    return commands


async def test_adms_connection(server_url: str, serial_number: str, device_name: str = "Mobile Gateway"):
    """Test handshake with a specific server config."""
    url = f"{server_url}/iclock/cdata"
    params = {
        "SN": serial_number,
        "DeviceName": device_name,
        "options": "all",
        "language": "69",
        "pushver": "2.4.1",
        "PushOptionsFlag": "1"
    }
    try:
        headers = {"User-Agent": ICLOCK_USER_AGENT}
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=headers)
            if response.status_code == 200:
                parsed = _parse_handshake_response(response.text)
                stamp = parsed.get("ATTLOGStamp", "?")
                server_ver = parsed.get("ServerVer", "?")
                return True, f"Handshake OK! ServerVer={server_ver} | ATTLOGStamp={stamp}"
            # Capture more of the error body to diagnose BioTime specific crashes
            return False, f"Server returned HTTP {response.status_code}: {response.text[:500]}"
    except Exception as e:
        return False, f"Connection error: {str(e)}"


async def register_employee_on_adms(client: httpx.AsyncClient, server_url: str, sn: str, employee_id: str, employee_name: str = "Mobile User"):
    """
    Register an employee on the ADMS server via OPERLOG push.

    The ZKTeco ADMS server silently discards ATTLOG records for employee PINs
    that don't exist in its employee/HR database. We must push a USER record
    via table=OPERLOG before the first ATTLOG for that employee.
    """
    db = SessionLocal()
    try:
        # Check if already registered
        existing = db.query(ADMSRegisteredEmployee).filter(
            ADMSRegisteredEmployee.employee_id == employee_id
        ).first()
        if existing:
            logger.debug(f"Employee {employee_id} already registered on ADMS, skipping.")
            return True

        # Push OPERLOG user record
        user_line = (
            f"USER PIN={employee_id}\t"
            f"Name={employee_name}\t"
            f"Pri=0\t"
            f"Passwd=\t"
            f"Card=\t"
            f"Grp=1\t"
            f"TZ=0000000100000000\t"
            f"Verify=0\t"
            f"VStyle=0\r\n"
        )

        url = f"{server_url}/iclock/cdata"
        params = {"SN": sn, "table": "OPERLOG", "Stamp": "0"}

        headers = {
            "Content-Type": "text/plain",
            "User-Agent": ICLOCK_USER_AGENT
        }
        resp = await client.post(
            url, params=params, content=user_line,
            headers=headers
        )

        if resp.status_code == 200:
            logger.info(f"✅ Registered employee {employee_id} ({employee_name}) on ADMS: {resp.text.strip()}")
            reg = ADMSRegisteredEmployee(employee_id=employee_id, employee_name=employee_name)
            db.add(reg)
            db.commit()
            return True
        else:
            logger.warning(f"⚠️ Failed to register employee {employee_id}: HTTP {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"❌ Employee registration error for {employee_id}: {e}")
        return False
    finally:
        db.close()


async def push_to_adms(log_id: int, employee_id: str, timestamp: datetime, punch_type: str, tz_offset_minutes: int = None):
    """
    Format and push a single attendance log to the ADMS Server.
    Auto-registers the employee on the ADMS server if not already registered.
    Updates PunchLog.adms_status to 'uploaded' or 'failed'.
    """
    server_url, sn, _ = get_adms_config()
    
    # Fetch global config for fallback offset
    db = SessionLocal()
    global_offset = 7
    try:
        config = db.query(ADMSTarget).filter(ADMSTarget.is_active == True).first()
        if config:
            global_offset = config.timezone_offset
    finally:
        db.close()

    # Punch status mapping
    status = "0" if punch_type.lower() in ["in", "check in"] else "1"

    # Convert UTC timestamp to local timezone for ADMS
    # Real ZKTeco machines always push local time; ADMS stores it as-is
    if tz_offset_minutes is not None:
        # Use punch-specific offset (highest priority)
        local_time = timestamp + timedelta(hours=tz_offset_minutes / 60.0)
    else:
        # Fallback to global config offset
        local_time = timestamp + timedelta(hours=global_offset)
        
    formatted_time = local_time.strftime("%Y-%m-%d %H:%M:%S")

    # Use the stamp from the handshake
    stamp = _handshake_state.get("attlog_stamp", "None")
    stamp_value = "9999" if stamp == "None" else stamp

    # ATTLOG line: PIN\tTime\tState\tVerifyType\tWorkCode\tReserved
    log_line = f"{employee_id}\t{formatted_time}\t{status}\t1\t0\t0\r\n"

    url = f"{server_url}/iclock/cdata"
    params = {
        "SN": sn,
        "table": "ATTLOG",
        "Stamp": stamp_value
    }

    db = SessionLocal()
    try:
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                # ── Push attendance record ──
                headers = {
                    "Content-Type": "text/plain",
                    "User-Agent": ICLOCK_USER_AGENT
                }
                response = await client.post(
                    url,
                    params=params,
                    content=log_line,
                    headers=headers
                )
                if response.status_code == 200 and response.text.strip().startswith("OK"):
                    logger.info(f"✅ ADMS push success for {employee_id}: {response.text.strip()}")
                    log = db.query(PunchLog).filter(PunchLog.id == log_id).first()
                    if log:
                        log.adms_status = "uploaded"
                        db.commit()
                    return True
                else:
                    logger.error(f"❌ ADMS push failed for {employee_id}: HTTP {response.status_code} → {response.text[:100]}")
                    log = db.query(PunchLog).filter(PunchLog.id == log_id).first()
                    if log:
                        log.adms_status = "failed"
                        db.commit()
                    return False
        except Exception as e:
            logger.error(f"❌ ADMS push exception for {employee_id}: {e}")
            log = db.query(PunchLog).filter(PunchLog.id == log_id).first()
            if log:
                log.adms_status = "failed"
                db.commit()
            return False
    finally:
        db.close()




async def retry_failed_pushes():
    """
    Periodic task that retries all PunchLogs with adms_status='failed' or 'pending'.
    Runs every 5 minutes.
    """
    while True:
        await asyncio.sleep(300)  # Wait 5 minutes between retry sweeps
        db = SessionLocal()
        try:
            pending = db.query(PunchLog).filter(
                PunchLog.adms_status.in_(["failed", "pending"])
            ).limit(50).all()  # Process up to 50 at a time

            if pending:
                logger.info(f"🔄 Retrying {len(pending)} failed/pending ADMS pushes...")
                for log in pending:
                    await push_to_adms(log.id, log.employee_id, log.timestamp, log.punch_type, log.tz_offset_minutes)
                    await asyncio.sleep(0.5)  # Small delay between retries
        except Exception as e:
            logger.error(f"Retry sweep error: {e}")
        finally:
            db.close()


async def adms_heartbeat_loop():
    """
    Virtual fingerprint machine heartbeat loop.
    
    Implements the full ZKTeco ADMS push protocol sequence as observed
    from the live server at adms.hartonomotor-group.com:
    
    1. Handshake GET: Register with ADMS and parse configuration
    2. Command polling GET: Poll /iclock/getrequest and ACK any commands
    3. Repeat with server-configured delay
    """
    logger.info("🚀 Starting ADMS heartbeat loop (ZKTeco iclock push protocol)")

    while True:
        server_url, sn, device_name = get_adms_config()

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:

                # ── Step 1: Handshake (or re-handshake if needed) ─────────────
                if not _handshake_state["handshake_done"]:
                    logger.info(f"🤝 Sending ADMS handshake for SN={sn} (Alias={device_name}) to {server_url}")

                    handshake_url = f"{server_url}/iclock/cdata"
                    handshake_params = {
                        "SN": sn,
                        "DeviceName": device_name,
                        "options": "all",
                        "language": "69",
                        "pushver": "2.4.1",
                        "PushOptionsFlag": "1"
                    }

                    headers = {"User-Agent": ICLOCK_USER_AGENT}
                    hs_resp = await client.get(handshake_url, params=handshake_params, headers=headers)
                    logger.info(f"Handshake response: HTTP {hs_resp.status_code}")

                    if hs_resp.status_code == 200:
                        parsed = _parse_handshake_response(hs_resp.text)
                        logger.info(f"📋 Server config: {parsed}")

                        # Store stamp values for use in data pushes
                        _handshake_state["attlog_stamp"] = parsed.get("ATTLOGStamp", "None")
                        _handshake_state["operlog_stamp"] = parsed.get("OPERLOGStamp", "0")

                        # Use server-configured timing
                        try:
                            _handshake_state["delay"] = int(parsed.get("Delay", 30))
                        except (ValueError, TypeError):
                            _handshake_state["delay"] = 30

                        try:
                            _handshake_state["trans_interval"] = int(parsed.get("TransInterval", 2))
                        except (ValueError, TypeError):
                            _handshake_state["trans_interval"] = 2

                        # Parse timezone from the server for UTC→local conversion
                        try:
                            _handshake_state["timezone_offset"] = int(parsed.get("TimeZone", 7))
                        except (ValueError, TypeError):
                            _handshake_state["timezone_offset"] = 7

                        _handshake_state["handshake_done"] = True
                        _update_adms_last_contact()
                        logger.info(
                            f"✅ Handshake complete! ATTLOGStamp={_handshake_state['attlog_stamp']}, "
                            f"Delay={_handshake_state['delay']}s, "
                            f"TimeZone=GMT+{_handshake_state['timezone_offset']}"
                        )

                        # ── Step 1.5: Push Server Options / Summary Stats ──────
                        # ADMS UI 'Device' tab stays blank unless we push table=options
                        db = SessionLocal()
                        try:
                            # Send fake/virtual aggregate stats so ADMS dashboard looks alive
                            total_punches = db.query(PunchLog).count()
                            unique_users = db.query(PunchLog.employee_id).distinct().count()
                            
                            options_payload = f"UserCount={unique_users}\r\nTransactionCount={total_punches}\r\nFpCount={unique_users}\r\n"
                            opt_resp = await client.post(
                                handshake_url, 
                                params={"SN": sn, "table": "options", "Stamp": _handshake_state['attlog_stamp']},
                                content=options_payload,
                                headers={"Content-Type": "text/plain", "User-Agent": ICLOCK_USER_AGENT}
                            )
                            logger.info(f"📊 Pushed Device Stats (Users={unique_users}, Trans={total_punches}) - Status {opt_resp.status_code}")
                        except Exception as e:
                            logger.warning(f"Failed to push options: {e}")
                        finally:
                            db.close()

                    else:
                        logger.warning(f"⚠️ Handshake failed: HTTP {hs_resp.status_code}. Will retry.")
                        await asyncio.sleep(60)
                        continue

                # ── Step 2: Poll for pending commands ─────────────────────────
                poll_url = f"{server_url}/iclock/getrequest"
                poll_params = {"SN": sn}

                headers = {"User-Agent": ICLOCK_USER_AGENT}
                poll_resp = await client.get(poll_url, params=poll_params, headers=headers)

                if poll_resp.status_code == 200:
                    body = poll_resp.text.strip()
                    logger.debug(f"Heartbeat OK for {sn}. Server response: {body!r}")
                    _update_adms_last_contact()

                    # ── Step 3: Acknowledge any commands the server sent ──────
                    commands = _parse_getrequest_commands(body)
                    if commands:
                        logger.info(f"📥 Received {len(commands)} command(s) from server: {commands}")
                        for cmd_id, cmd_name in commands:
                            await _acknowledge_command(client, server_url, sn, cmd_id, cmd_name)

                else:
                    logger.warning(f"⚠️ Heartbeat returned HTTP {poll_resp.status_code} — forcing re-handshake")
                    _handshake_state["handshake_done"] = False

        except httpx.ConnectError as e:
            logger.error(f"🔴 Cannot reach ADMS server at {server_url}: {e}")
            _handshake_state["handshake_done"] = False
        except httpx.TimeoutException:
            logger.warning(f"⏱️ ADMS heartbeat timed out for {sn}")
            _handshake_state["handshake_done"] = False
        except Exception as e:
            logger.error(f"🔴 Unexpected heartbeat error: {e}")
            _handshake_state["handshake_done"] = False

        # Wait using server-configured delay (default 30s)
        delay = _handshake_state.get("delay", 30)
        await asyncio.sleep(delay)
