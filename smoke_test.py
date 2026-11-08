"""Local smoke test: webhook auth, business flow, dedup, owner-notify, admin auth.

Run:  python smoke_test.py   (from the project root; uses a local SQLite DB)
"""
import base64
import logging
import os

# Must be set BEFORE importing the app (config.py reads env at import time).
os.environ["WEBHOOK_SECRET"] = "test-secret"
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test-pass"
os.environ.pop("DATABASE_URL", None)  # use the local SQLite fallback

logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

from app import telegram_client as tg  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Lead  # noqa: E402

init_db()
client = TestClient(app)
HEADER = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
app.state  # noqa

# --- 1. Webhook security ---
r = client.post("/webhook", json={})
assert r.status_code == 403, r.status_code
r = client.post("/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
assert r.status_code == 403
r = client.post("/webhook", json={}, headers=HEADER)
assert r.status_code == 200
print("webhook auth OK")

# --- Mock Telegram API and capture outgoing calls ---
sent: list[tuple[str, dict]] = []
tg.call = lambda method, payload: sent.append((method, payload)) or {}

# --- 2. business_connection update is stored ---
r = client.post(
    "/webhook",
    json={"business_connection": {"id": "conn1", "user_chat_id": 111, "is_enabled": True}},
    headers=HEADER,
)
assert r.json() == {"ok": True}
print("business_connection stored OK")

# --- 3. Welcome message with buttons, carries business_connection_id ---
client.post(
    "/webhook",
    json={"business_message": {"business_connection_id": "conn1", "chat": {"id": 222, "type": "private"},
          "from": {"id": 222, "first_name": "Ali", "is_bot": False}, "text": "salom"}},
    headers=HEADER,
)
method, payload = sent[-1]
assert method == "sendMessage"
assert payload.get("business_connection_id") == "conn1"
assert "Assalomu alaykum Ali" in payload["text"]
assert "inline_keyboard" in payload["reply_markup"]
print("welcome + course keyboard OK")

# --- 4. Owner's own messages are ignored ---
n = len(sent)
client.post(
    "/webhook",
    json={"business_message": {"business_connection_id": "conn1", "chat": {"id": 111, "type": "private"},
          "from": {"id": 111, "is_bot": False}, "text": "salom"}},
    headers=HEADER,
)
assert len(sent) == n, "owner got a reply!"
print("owner messages ignored OK")

# --- 5. Group chat messages are ignored ---
client.post(
    "/webhook",
    json={"business_message": {"business_connection_id": "conn1", "chat": {"id": -100, "type": "supergroup"},
          "from": {"id": 5, "is_bot": False}, "text": "salom"}},
    headers=HEADER,
)
assert len(sent) == n
print("group message ignored OK")

# --- 6. Full flow: button -> name -> phone ---
client.post(
    "/webhook",
    json={"callback_query": {"id": 1, "business_connection_id": "conn1",
          "from": {"id": 222, "first_name": "Ali"},
          "message": {"chat": {"id": 222, "type": "private"}}, "data": "course:2"}},
    headers=HEADER,
)
assert "Ismingizni yozib qoldiring" in sent[-1][1]["text"], sent[-1][1]["text"]
last_send = [p for m, p in sent if m == "sendMessage"][-1]
assert last_send.get("business_connection_id") == "conn1"

def business_text(text: str) -> None:
    client.post(
        "/webhook",
        json={"business_message": {"business_connection_id": "conn1", "chat": {"id": 222, "type": "private"},
              "from": {"id": 222, "first_name": "Ali", "username": "ali", "is_bot": False}, "text": text}},
        headers=HEADER,
    )

business_text("Ali Valiyev")
assert "Telefon raqamingizni qoldiring" in sent[-1][1]["text"]
business_text("99 123 45 67")  # spaced local format
assert "qabul qilindi" in sent[-1][1]["text"]

with SessionLocal() as s:
    lead = s.query(Lead).filter(Lead.telegram_user_id == 222).one()
    assert lead.phone == "+998991234567", lead.phone
    assert lead.course == "Buxgalteriya 0 dan", lead.course
    assert lead.full_name == "Ali Valiyev", lead.full_name
print("full flow OK ->", lead.full_name, lead.phone, lead.course)

# --- 7. Dedup: same user again -> still exactly one lead ---
business_text("/start")
business_text("1")
business_text("Ali Valiyev")
business_text("901234567")
with SessionLocal() as s:
    count = s.query(Lead).filter(Lead.phone == "+998901234567").count()
    assert count == 1, count
print("dedup OK")

# --- 8. Admin panel auth ---
auth = {"Authorization": "Basic " + base64.b64encode(b"admin:test-pass").decode()}
r = client.get("/admin/leads", follow_redirects=False)
assert r.status_code == 401, r.status_code
r = client.get("/admin/leads", headers=auth, follow_redirects=False)
assert r.status_code == 200, r.status_code
assert "Lidlar" in r.text
# courses page renders
r = client.get("/admin/courses", headers=auth)
assert r.status_code == 200 and "Kurslar" in r.text
# messages page renders all 7 templates
r = client.get("/admin/messages", headers=auth)
assert r.status_code == 200 and r.text.count("<textarea") == 7
# settings page renders
r = client.get("/admin/settings", headers=auth)
assert r.status_code == 200 and "Owner chat ID" in r.text
# CSV export works
r = client.get("/admin/leads/export", headers=auth)
assert r.status_code == 200 and "Ism" in r.text
print("admin pages OK")

print("ALL_SMOKE_TESTS_PASSED")
