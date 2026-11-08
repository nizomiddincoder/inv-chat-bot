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
os.environ["OWNER_CHAT_ID"] = "111"  # the business owner is also the bot admin
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
# send_message uses tg._post directly (to inspect errors), so mock _post.
sent: list[tuple[str, dict]] = []
tg._post = lambda method, payload: (sent.append((method, payload)) or {}, None)

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

# --- 6. Full flow: button -> name -> (edit name) -> phone ---
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

# --- 6b. edited_business_message: user fixes the name while waiting for phone ---
client.post(
    "/webhook",
    json={"edited_business_message": {"business_connection_id": "conn1", "chat": {"id": 222, "type": "private"},
          "from": {"id": 222, "first_name": "Ali", "is_bot": False}, "text": "Aziza Karimova"}},
    headers=HEADER,
)
assert "tahrirlandi" in sent[-1][1]["text"], sent[-1][1]["text"]
assert sent[-1][1].get("business_connection_id") == "conn1"
print("edited_business_message (name fix) OK")

business_text("99 123 45 67")  # spaced local format
all_texts = [p.get("text", "") for m, p in sent if m == "sendMessage"]
assert any("qabul qilindi" in t for t in all_texts), all_texts[-1]
# owner receives the new-lead notification
owner_notifs = [p for m, p in sent if m == "sendMessage" and p.get("chat_id") == 111]
assert any("Yangi lid" in p["text"] for p in owner_notifs), owner_notifs
print("owner notification OK")

with SessionLocal() as s:
    lead = s.query(Lead).filter(Lead.telegram_user_id == 222).one()
    assert lead.phone == "+998991234567", lead.phone
    assert lead.course == "Buxgalteriya 0 dan", lead.course
    assert lead.full_name == "Aziza Karimova", lead.full_name
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

# --- 7b. Owner-only admin commands via the business chat ---
n = len(sent)
client.post(
    "/webhook",
    json={"business_message": {"business_connection_id": "conn1", "chat": {"id": 111, "type": "private"},
          "from": {"id": 111, "is_bot": False}, "text": "/lidlar"}},
    headers=HEADER,
)
assert len(sent) > n, "owner got no reply to /lidlar"
assert sent[-1][0] == "sendMessage"
assert "Lidlar statistikasi" in sent[-1][1]["text"], sent[-1][1]["text"]
# The owner's own chat is NOT a business peer: the reply must NOT carry
# business_connection_id (that caused BUSINESS_PEER_INVALID in production).
assert sent[-1][1].get("business_connection_id") is None, sent[-1][1]
assert sent[-1][1].get("chat_id") == 111

# owner's normal chatter is still ignored (restriction is only for commands)
n = len(sent)
client.post(
    "/webhook",
    json={"business_message": {"business_connection_id": "conn1", "chat": {"id": 111, "type": "private"},
          "from": {"id": 111, "is_bot": False}, "text": "salom do'stim"}},
    headers=HEADER,
)
assert len(sent) == n, "owner's non-command message got a reply!"
print("owner admin command OK (normal chatter still ignored)")

# /kurslar and /yordam for the owner
client.post(
    "/webhook",
    json={"business_message": {"business_connection_id": "conn1", "chat": {"id": 111, "type": "private"},
          "from": {"id": 111, "is_bot": False}, "text": "/kurslar"}},
    headers=HEADER,
)
assert "Kurslar" in sent[-1][1]["text"]
print("/kurslar OK")

# --- 7c. Non-owner gets NO admin commands: /lidlar from a stranger is rejected ---
client.post(
    "/webhook",
    json={"message": {"chat": {"id": 999, "type": "private"},
          "from": {"id": 999, "first_name": "Stranger", "is_bot": False}, "text": "/lidlar"}},
    headers=HEADER,
)
assert "faqat admin uchun" in sent[-1][1]["text"], sent[-1][1]["text"]
# and the normal flow still works for that stranger afterwards
client.post(
    "/webhook",
    json={"message": {"chat": {"id": 999, "type": "private"},
          "from": {"id": 999, "first_name": "Stranger", "is_bot": False}, "text": "salom"}},
    headers=HEADER,
)
assert "Assalomu alaykum" in sent[-1][1]["text"], sent[-1][1]["text"]
print("admin command blocked for non-owner OK (flow unaffected)")

# --- 7d. BUSINESS_PEER_INVALID fallback: customer reply is retried without conn id ---
calls: list[dict] = []

def fake_post(method, payload):
    calls.append(payload)
    if method == "sendMessage" and payload.get("business_connection_id") == "conn1":
        return None, "Bad Request: BUSINESS_PEER_INVALID"  # first attempt fails
    return {"message_id": 1}, None  # retry without conn id succeeds

tg._post = fake_post
business_text("/start")
assert any("business_connection_id" not in p for p in calls), "no retry happened"
assert calls[-1].get("business_connection_id") is None
assert "Assalomu alaykum" in calls[-1]["text"]
tg._post = lambda method, payload: (sent.append((method, payload)) or {}, None)  # restore mock
print("BUSINESS_PEER_INVALID fallback OK")

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
# messages page renders all 8 templates
r = client.get("/admin/messages", headers=auth)
assert r.status_code == 200 and r.text.count("<textarea") == 8
# settings page renders
r = client.get("/admin/settings", headers=auth)
assert r.status_code == 200 and "Owner chat ID" in r.text
# CSV export works
r = client.get("/admin/leads/export", headers=auth)
assert r.status_code == 200 and "Ism" in r.text
print("admin pages OK")

print("ALL_SMOKE_TESTS_PASSED")
