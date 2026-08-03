import json
from typing import Optional
from pywebpush import webpush, WebPushException
from config import config
from models.schemas import PushSubscription


def send_push_notification(
    subscription: PushSubscription,
    title: str,
    body: str,
    url: Optional[str] = None,
) -> bool:
    """
    Send a push notification to a subscribed client.

    Args:
        subscription: Push subscription data from the client
        title: Notification title
        body: Notification body text
        url: Optional URL to open when notification is clicked

    Returns:
        True if notification was sent successfully
    """
    if not config.VAPID_PRIVATE_KEY or not config.VAPID_PUBLIC_KEY:
        raise ValueError("VAPID keys not configured")

    subscription_info = {
        "endpoint": subscription.endpoint,
        "keys": {
            "p256dh": subscription.keys.p256dh,
            "auth": subscription.keys.auth,
        }
    }

    payload = {
        "title": title,
        "body": body,
        "icon": "/assets/icon-192.png",
        "badge": "/assets/icon-192.png",
    }

    if url:
        payload["data"] = {"url": url}

    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=config.VAPID_PRIVATE_KEY,
            vapid_claims={
                "sub": f"mailto:{config.VAPID_CLAIM_EMAIL}"
            }
        )
        return True
    except WebPushException as e:
        print(f"Push notification failed: {e}")
        return False
