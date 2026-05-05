"""
Firebase Cloud Messaging (FCM) notification service for push notifications.

Sends push notifications to registered mobile devices for clock-in reminders
and other attendance-related alerts.
"""
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ─── FCM Configuration ─────────────────────────────────────────────────────────

FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY", "")
FCM_API_URL = "https://fcm.googleapis.com/fcm/send"


def is_fcm_configured() -> bool:
    """Check if FCM server key is present in environment."""
    return bool(FCM_SERVER_KEY)


# ─── Send Notification ─────────────────────────────────────────────────────────

def send_push_notification(
    fcm_token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """
    Send a push notification to a single device via FCM.

    Args:
        fcm_token: The device's FCM registration token.
        title: Notification title.
        body: Notification body text.
        data: Optional custom data payload (key-value pairs).

    Returns:
        True if the notification was accepted by FCM, False otherwise.
    """
    if not is_fcm_configured():
        logger.warning("FCM not configured — cannot send push notification.")
        return False

    if not fcm_token:
        logger.warning("No FCM token provided — skipping notification.")
        return False

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
        response = requests.post(FCM_API_URL, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            result = response.json()
            if result.get("success") == 1:
                logger.info(f"FCM notification sent to device. Response: {result}")
                return True
            else:
                logger.error(f"FCM send returned failure: {result}")
                return False
        else:
            logger.error(
                f"FCM HTTP {response.status_code}: {response.text}"
            )
            return False
    except requests.RequestException as e:
        logger.error(f"FCM request failed: {e}")
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
