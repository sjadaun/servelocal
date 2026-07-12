"""
push.py
Web Push notifications: VAPID key management, and sending pushes to every
subscribed device. Used by app.py (subscribe/unsubscribe API + serving the
VAPID public key to the browser) and by the background scheduler that fires
"start prep by" reminders.

Platform notes (this matters a lot for who actually gets notified):
  - Android (Chrome/Edge/Firefox): works from a normal bookmarked tab, no
    installation needed.
  - iOS/iPadOS Safari: web push ONLY works if the site has been added to
    the Home Screen (Share -> Add to Home Screen) and opened from that
    icon at least once, on iOS 16.4+. A regular Safari tab or bookmark
    will NOT receive push notifications -- there's no way around this,
    it's an Apple platform restriction, not something this app can change.
"""

import json
import pathlib
import datetime

from py_vapid import Vapid02
from pywebpush import webpush, WebPushException

import database as db

VAPID_KEY_FILE = pathlib.Path(__file__).parent / "vapid_private_key.pem"
# Required by the Web Push protocol (a contact point push services can use
# to reach the sender if something's wrong) -- not emailed anywhere by us.
VAPID_CLAIMS_SUB = "mailto:servelocal@localhost"


def _load_or_create_vapid() -> Vapid02:
    if VAPID_KEY_FILE.exists():
        return Vapid02.from_file(str(VAPID_KEY_FILE))
    v = Vapid02()
    v.generate_keys()
    v.save_key(str(VAPID_KEY_FILE))
    print(f"[push] generated new VAPID keypair at {VAPID_KEY_FILE}")
    return v


_vapid = _load_or_create_vapid()


def get_public_key_b64() -> str:
    """URL-safe base64, no padding -- the format the browser's
    PushManager.subscribe({applicationServerKey: ...}) expects."""
    import base64
    nums = _vapid.public_key.public_numbers()
    raw = b"\x04" + nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def save_subscription(sub: dict, label: str = ""):
    """sub is the raw PushSubscription.toJSON() object from the browser:
    {"endpoint": "...", "keys": {"p256dh": "...", "auth": "..."}}"""
    db.upsert_push_subscription(sub["endpoint"], json.dumps(sub["keys"]), label)


def remove_subscription(endpoint: str):
    db.delete_push_subscription(endpoint)


def send_to_all(title: str, body: str, tag: str = None, url: str = "/") -> int:
    """Sends one notification to every stored subscription (every device
    that's enabled notifications). Prunes subscriptions the push service
    reports as dead (expired/uninstalled). Returns how many sends succeeded."""
    payload = json.dumps({"title": title, "body": body, "tag": tag, "url": url})
    sent = 0
    dead_endpoints = []

    for sub in db.list_push_subscriptions():
        keys = json.loads(sub["keys_json"])
        try:
            webpush(
                subscription_info={"endpoint": sub["endpoint"], "keys": keys},
                data=payload,
                vapid_private_key=str(VAPID_KEY_FILE),
                vapid_claims={"sub": VAPID_CLAIMS_SUB},
                ttl=300,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            print(f"[push] send failed (HTTP {status}): {exc}")
            if status in (404, 410):  # gone / not found -> this subscription is dead
                dead_endpoints.append(sub["endpoint"])
        except Exception as exc:
            print(f"[push] send error: {exc}")

    for endpoint in dead_endpoints:
        remove_subscription(endpoint)

    return sent


def check_and_send_prep_reminders():
    """Call this periodically (see app.py's background scheduler). Finds
    any occurrence whose 'start prep by' / 'leave by' moment just arrived
    and hasn't been notified yet, sends it, and marks it sent so it's never
    repeated."""
    for occ in db.get_due_prep_notifications():
        meal = occ["meal"]
        going_out = bool(meal["going_out"])
        label = "Time to leave" if going_out else "Time to start prepping"
        body = meal["name"]
        if going_out and meal["going_out_place"]:
            body += f" @ {meal['going_out_place']}"
        body += f" -- ready by {meal['scheduled_time']}"

        sent = send_to_all(title=label, body=body, tag=f"prep-{occ['slot_id']}", url="/")
        occ_date = occ["when"].date().isoformat()
        db.mark_notification_sent(occ["slot_id"], occ_date, "prep")
        print(f"[push] prep reminder for {meal['name']!r} sent to {sent} device(s)")
