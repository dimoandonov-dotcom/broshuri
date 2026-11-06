"""Известия на телефона (Web Push) - вместо Telegram. Работят, когато приложението е добавено
на началния екран (iPhone с iOS 16.4+) или в Chrome/Edge на компютър и Android."""
from __future__ import annotations

import json
import os

from . import db

PUB, PRIV = os.getenv("VAPID_PUBLIC", ""), os.getenv("VAPID_PRIVATE", "")
SUB = os.getenv("VAPID_SUBJECT", "mailto:promo@trista.bg")

SCHEMA = """CREATE TABLE IF NOT EXISTS push_subs (
  endpoint TEXT PRIMARY KEY, username TEXT NOT NULL, data TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""


def init():
    db.q(SCHEMA)


def subscribe(username: str, sub: dict):
    db.q("""INSERT INTO push_subs (endpoint, username, data) VALUES (:e, :u, :d)
            ON CONFLICT (endpoint) DO UPDATE SET username=:u, data=:d""",
         e=sub["endpoint"], u=username, d=json.dumps(sub))


def send(usernames: list[str], title: str, body: str, url: str = "/") -> int:
    if not (PUB and PRIV) or not usernames:
        return 0
    from pywebpush import WebPushException, webpush
    n = 0
    for r in db.rows("SELECT endpoint, data FROM push_subs WHERE username = ANY(:u)", u=usernames):
        try:
            webpush(subscription_info=json.loads(r["data"]),
                    data=json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False),
                    vapid_private_key=PRIV, vapid_claims={"sub": SUB}, ttl=86400)
            n += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                db.q("DELETE FROM push_subs WHERE endpoint=:e", e=r["endpoint"])
            print("push:", e, flush=True)
        except Exception as e:  # noqa: BLE001
            print("push:", e, flush=True)
    return n
