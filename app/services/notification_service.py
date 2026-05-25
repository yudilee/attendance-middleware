"""
Firebase Cloud Messaging (FCM) notification service for push notifications.

Sends push notifications to registered mobile devices for clock-in reminders
and other attendance-related alerts. Supports FCM v1 API with legacy fallback.
"""
import logging
import os
import json
from typing import Optional
from datetime import datetime, timedelta
import requests

logger = logging.getLogger(__name__)

# ─── FCM Configuration ─────────────────────────────────────────────────────────

FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY", "")
FCM_SERVICE_ACCOUNT_PATH = os.getenv("FCM_SERVICE_ACCOUNT_PATH", "")

_credentials = None
_project_id = None
_access_token = None
_token_expiry = None


def load_service_account() -> bool:
    """Load Firebase service account credentials."""
    global _credentials, _project_id
    
    # 1. Try loading from raw JSON string (ideal for Docker environment variables!)
    json_str = os.getenv("FCM_SERVICE_ACCOUNT_JSON", "")
    if json_str:
        try:
            from google.oauth2 import service_account
            SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]
            info = json.loads(json_str)
            _credentials = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
            _project_id = info.get("project_id")
            return True
        except Exception as e:
            logger.error(f"Failed to load FCM service account from FCM_SERVICE_ACCOUNT_JSON env: {e}")

    # 2. Try loading from file path
    path = FCM_SERVICE_ACCOUNT_PATH or os.getenv("FCM_SERVICE_ACCOUNT_PATH", "")
    if not path or not os.path.exists(path):
        return False
    try:
        from google.oauth2 import service_account
        SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]
        _credentials = service_account.Credentials.from_service_account_file(
            path, scopes=SCOPES
        )
        with open(path, "r") as f:
            data = json.load(f)
            _project_id = data.get("project_id")
        return True
    except Exception as e:
        logger.error(f"Failed to load FCM service account from {path}: {e}")
        return False


def get_access_token() -> Optional[str]:
    """Fetch cached OAuth 2.0 access token for FCM v1."""
    global _access_token, _token_expiry, _credentials
    
    if not _credentials:
        if not load_service_account():
            return None
            
    # Refresh token if not set or expiring in less than 5 minutes
    if not _access_token or not _token_expiry or datetime.utcnow() + timedelta(minutes=5) > _token_expiry:
        try:
            from google.auth.transport.requests import Request as AuthRequest
            _credentials.refresh(AuthRequest())
            _access_token = _credentials.token
            _token_expiry = _credentials.expiry
        except Exception as e:
            logger.error(f"Failed to refresh FCM access token: {e}")
            return None
    return _access_token


def is_fcm_configured() -> bool:
    """Check if FCM v1 or legacy key is present."""
    if FCM_SERVICE_ACCOUNT_PATH and os.path.exists(FCM_SERVICE_ACCOUNT_PATH):
        return True
    return bool(FCM_SERVER_KEY)


# ─── Send Notification ─────────────────────────────────────────────────────────

def send_push_notification(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """
    Send a push notification to a single device via FCM (v1 with legacy fallback).

    Args:
        fcm_token: The device's FCM registration token.
        title: Notification title.
        body: Notification body text.
        data: Optional custom data payload (key-value pairs).

    Returns:
        True if the notification was accepted by FCM, False otherwise.
    """
    if not fcm_token:
        logger.warning("No FCM token provided — skipping notification.")
        return False

    token = get_access_token()
    if token and _project_id:
        # Use FCM v1 API
        v1_url = f"https://fcm.googleapis.com/v1/projects/{_project_id}/messages:send"
        
        # Ensure all data values are strings for FCM v1
        string_data = {}
        if data:
            for k, v in data.items():
                string_data[str(k)] = str(v)

        payload = {
            "message": {
                "token": fcm_token,
                "notification": {
                    "title": title,
                    "body": body,
                },
            }
        }
        if string_data:
            payload["message"]["data"] = string_data

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(v1_url, json=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                logger.info(f"FCM v1 notification sent successfully: {response.json()}")
                return True
            else:
                logger.error(f"FCM v1 returned HTTP {response.status_code}: {response.text}")
                return False
        except requests.RequestException as e:
            logger.error(f"FCM v1 request failed: {e}")
            return False
    
    # Graceful fallback to Legacy FCM API
    if FCM_SERVER_KEY:
        legacy_url = "https://fcm.googleapis.com/fcm/send"
        payload = {
            "to": fcm_token,
            "notification": {
                "title": title,
                "body": body,
                "sound": "default",
                "priority": "high",
            },
        }

        if data:
            payload["data"] = data

        headers = {
            "Authorization": f"key={FCM_SERVER_KEY}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(legacy_url, json=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                result = response.json()
                if result.get("success") == 1:
                    logger.info(f"FCM legacy notification sent successfully: {result}")
                    return True
                else:
                    logger.error(f"FCM legacy returned failure: {result}")
                    return False
            else:
                logger.error(f"FCM legacy returned HTTP {response.status_code}: {response.text}")
                return False
        except requests.RequestException as e:
            logger.error(f"FCM legacy request failed: {e}")
            return False

    logger.warning("FCM not configured (no service account or legacy server key) — cannot send push notification.")
    return False


# ─── Clock-In Reminder ─────────────────────────────────────────────────────────

def send_clock_in_reminder(fcm_token: str) -> bool:
    """
    Send a clock-in reminder notification to a single device.

    Args:
        fcm_token: The device's FCM registration token.

    Returns:
        True if the notification was sent successfully.
    """
    return send_push_notification(
        fcm_token=fcm_token,
        title="⏰ Clock-In Reminder",
        body="Don't forget to clock in! Your attendance is waiting.",
        data={"type": "clock_in_reminder"},
    )


# ─── Correction Result ─────────────────────────────────────────────────────────

def send_correction_result(fcm_token: str, is_approved: bool, log_id: Optional[int] = None) -> bool:
    """
    Send a push notification about a correction review result.
    
    Args:
        fcm_token: The device's FCM registration token.
        is_approved: True if approved, False if rejected.
        log_id: The ID of the original punch log, if any.
        
    Returns:
        True if the notification was sent successfully.
    """
    status = "approved" if is_approved else "rejected"
    title = f"Attendance Correction {status.title()}"
    body = f"Your attendance correction request has been {status}."
    
    data = {"type": "correction_result", "status": status}
    if log_id:
        data["log_id"] = str(log_id)
        
    return send_push_notification(
        fcm_token=fcm_token,
        title=title,
        body=body,
        data=data,
    )
