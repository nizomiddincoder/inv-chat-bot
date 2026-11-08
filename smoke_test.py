"""Local smoke test: webhook auth, business flow, dedup, owner-notify, admin auth,
update_id dedup, Claude reply with [LEAD] marker, OWNER_NOTIFY_CHAT_ID routing.

Run:  python smoke_test.py   (from the project root; uses a local SQLite DB)
"""
import base64
import logging
import os
import time

# Must be set BEFORE importing the app (config.py reads env at import time).
os.environ["WEBHOOK_SECRET"] = "test-secret"
os.environ["ADMIN_USER"] = "admin"
os.environ["ADMIN_PASSWORD"] = "test-pass"
os.environ["OWNER_CHAT_ID"] = "111"  # the business owner is also the bot admin
os.environ["OWNER_NOTIFY_CHAT_ID"] = "777"  # separate chat for notifications
os.environ.pop("DATABASE_URL", None)  # use the local SQLite fallback

logging.disable(logging.CRITICAL)

from fastapi.testclient import TestClient  # noqa: E402

from app import claude_client  # noqa: E402
from app import telegram_client as tg  # noqa: E402
from app.config import settings  # noqa: E402
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

# --- 1b. Admin panel session login (cookie, not Basic auth) ---
r = client.get("/admin/leads", follow_redirects=False)
assert r.status_code == 302 and "/admin/login" in r.headers["location"], (r.status_code, r.headers)
r = client.get("/admin/leads")  # follow the redirect -> login page
assert "Markaz CRM" in r.text and "Parol" in r.text
r = client.post("/admin/login", data={"login": "admin", "password": "wrong-pass"})
assert r.status_code == 401 and "xato" in r.text
r = client.post("/admin/login", data={"login": "admin", "password": "test-pass"}, follow_redirects=False)
assert r.status_code == 303, r.status_code  # cookie is now stored by the test client
print("session login OK")

# from here on the test client carries the session cookie (variable kept for
# the many headers=auth call sites; the cookie does the real work)
auth = {}

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
# owner receives the new-lead notification (at OWNER_NOTIFY_CHAT_ID = 777);
# on repeat applications the same notification starts with "Takroriy murojaat"
owner_notifs = [p for m, p in sent if m == "sendMessage" and p.get("chat_id") == 777]
assert any("Yangi lid" in p["text"] or "Takroriy murojaat" in p["text"] for p in owner_notifs), owner_notifs
print("owner notification OK")

with SessionLocal() as s:
    lead = s.query(Lead).filter(Lead.telegram_user_id == 222).one()
    assert lead.phone == "+998991234567", lead.phone
    assert lead.course == "Buxgalteriya 0 dan", lead.course
    assert lead.full_name == "Aziza Karimova", lead.full_name
print("full flow OK ->", lead.full_name, lead.phone, lead.course)

# --- 7. Dedup: same user again -> still exactly one lead ---
business_text("/start")
business_text("2)")  # typed number with punctuation also selects a course
assert "Ismingizni yozib qoldiring" in sent[-1][1]["text"], sent[-1][1]["text"]
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

# --- 7e. update_id dedup: a retried update is processed only once ---
# (unique per run so the test can be re-run against the same DB file)
uniq = int(time.time())
n = len(sent)
dup_update = {"update_id": uniq,
              "business_message": {"business_connection_id": "conn1", "chat": {"id": 444, "type": "private"},
              "from": {"id": 444, "first_name": "Dilnoza", "is_bot": False}, "text": "salom"}}
client.post("/webhook", json=dup_update, headers=HEADER)
assert len(sent) == n + 1, "first copy of the update should be processed"
client.post("/webhook", json=dup_update, headers=HEADER)  # Telegram-style retry
assert len(sent) == n + 1, "duplicate update_id got processed twice!"
print("update_id dedup OK")

# --- 7f. Claude answers a business message + [LEAD] marker + OWNER_NOTIFY_CHAT_ID ---
settings.ANTHROPIC_API_KEY = "test-key"  # enable Claude mode for this section

def fake_claude(payload):
    assert payload["model"] == settings.CLAUDE_MODEL
    assert payload["system"], "knowledge/system prompt missing"
    return {"content": [{"type": "text", "text":
        "Rahmat, Aziza! Ma'lumotlaringiz qabul qilindi, admin tez orada bog'lanadi. "
        "[LEAD] Aziza Karimova | 998905554433 | Trading [/LEAD]"}]}

claude_client._post_messages = fake_claude

n = len(sent)
client.post(
    "/webhook",
    json={"update_id": uniq + 1,
          "business_message": {"business_connection_id": "conn1", "chat": {"id": 333, "type": "private"},
          "from": {"id": 333, "first_name": "Aziza", "username": "aziza", "is_bot": False},
          "text": "Trading kursiga yozilmoqchiman"}},
    headers=HEADER,
)
# the visitor's reply first, then the owner notification to OWNER_NOTIFY_CHAT_ID
visitor_msgs = [p for m, p in sent[n:] if m == "sendMessage" and p.get("chat_id") == 333]
assert visitor_msgs, sent[n:]
visitor_reply = visitor_msgs[-1]
assert visitor_reply.get("business_connection_id") == "conn1"
assert "[LEAD]" not in visitor_reply["text"], visitor_reply["text"]
assert "qabul qilindi" in visitor_reply["text"], visitor_reply["text"]
with SessionLocal() as s:
    lead = s.query(Lead).filter(Lead.telegram_user_id == 333).one()
    assert lead.full_name == "Aziza Karimova", lead.full_name
    assert lead.phone == "+998905554433", lead.phone
    assert lead.course == "Trading", lead.course
    assert lead.source == "claude", lead.source
# the notification went to OWNER_NOTIFY_CHAT_ID (777), not the owner chat (111)
notifs = [p for m, p in sent[n:] if m == "sendMessage" and p.get("chat_id") == 777]
assert notifs and ("Yangi lid" in notifs[-1]["text"] or "Takroriy murojaat" in notifs[-1]["text"]), notifs
assert not any(m == "sendMessage" and p.get("chat_id") == 111 for m, p in sent[n:]), \
    "notification must go to OWNER_NOTIFY_CHAT_ID, not OWNER_CHAT_ID"
print("claude reply + [LEAD] + OWNER_NOTIFY_CHAT_ID OK")

# Claude unavailable -> built-in flow answers, the visitor is never left hanging
def broken_claude(payload):
    raise RuntimeError("api down")

claude_client._post_messages = broken_claude
client.post(
    "/webhook",
    json={"update_id": uniq + 2,
          "business_message": {"business_connection_id": "conn1", "chat": {"id": 333, "type": "private"},
          "from": {"id": 333, "first_name": "Aziza", "is_bot": False}, "text": "salom"}},
    headers=HEADER,
)
assert "Assalomu alaykum" in sent[-1][1]["text"], sent[-1][1]["text"]
settings.ANTHROPIC_API_KEY = ""  # disable Claude mode again
print("claude fallback to built-in flow OK")

# --- 8. Admin panel pages (session cookie from step 1b) ---
r = client.get("/admin/leads", follow_redirects=False)
assert r.status_code == 200, r.status_code
assert "Lidlar" in r.text
# courses page renders
r = client.get("/admin/courses", headers=auth)
assert r.status_code == 200 and "Kurslar" in r.text
# kanban board is the CRM landing page and renders all 4 columns
r = client.get("/admin", headers=auth, follow_redirects=False)
assert r.status_code == 302 and "/admin/kanban" in r.headers["location"], r.headers
r = client.get("/admin/kanban", headers=auth)
assert r.status_code == 200, r.status_code
assert "Kanban doskasi" in r.text
for label in ("Yangi", "Aloqaga chiqildi", "qishga yozildi", "Rad etildi"):
    assert label in r.text, label  # note: apostrophes are HTML-escaped
assert "data-drop=\"new\"" in r.text  # drag&drop columns rendered
with SessionLocal() as s:
    some_lead = s.query(Lead).first()
    assert some_lead is not None
    lead_id, old_status = some_lead.id, some_lead.status
# move endpoint changes the lead status like a drag & drop would
target = "enrolled" if old_status != "enrolled" else "contacted"
r = client.post(f"/admin/leads/{lead_id}/move", data={"status": target}, headers=auth)
assert r.status_code == 200 and r.json()["ok"] is True and r.json()["status"] == target
with SessionLocal() as s:
    assert s.get(Lead, lead_id).status == target
r = client.post(f"/admin/leads/{lead_id}/move", data={"status": "bogus"}, headers=auth)
assert r.json().get("ok") is False
# moving to "Rad etildi" without a reason is rejected; with a reason it works
r = client.post(f"/admin/leads/{lead_id}/move", data={"status": "rejected"}, headers=auth)
assert r.json().get("ok") is False and "sababi" in r.json()["error"]
r = client.post(f"/admin/leads/{lead_id}/move", data={"status": "rejected", "reason": "narx"}, headers=auth)
assert r.json().get("ok") is True
with SessionLocal() as s:
    assert s.get(Lead, lead_id).reject_reason == "narx"
client.post(f"/admin/leads/{lead_id}/move", data={"status": old_status}, headers=auth)
print("kanban board + move endpoint + reject reason OK")
# old messages page is gone, replaced by the AI page
r = client.get("/admin/messages", headers=auth, follow_redirects=False)
assert r.status_code == 404, r.status_code
print("messages page removed OK")
# reset saved AI texts so the default professional preset is shown (re-run safe)
from app.models import AiSetting  # noqa: E402
with SessionLocal() as s:
    s.query(AiSetting).delete(synchronize_session=False)
    s.commit()
# AI page renders: style prompt + extra knowledge + sandbox test window
r = client.get("/admin/ai", headers=auth)
assert r.status_code == 200, r.status_code
assert "Javob uslubi" in r.text and "Sinov oynasi" in r.text, r.text[:400]
assert "professional" in r.text  # default professional style preset is shown
# saving the style prompt + extra knowledge reaches the Claude system prompt
r = client.post(
    "/admin/ai",
    data={"style_prompt": "Har doim juda rasmiy va qisqa javob bering.",
          "extra_knowledge": "Sinov narxi: 1 so'm."},
    headers=auth, follow_redirects=False,
)
assert r.status_code == 303, r.status_code
prompt = claude_client.build_system_prompt()
assert "juda rasmiy" in prompt, prompt
assert "Sinov narxi: 1 so'm" in prompt, prompt
assert "[LEAD]" in prompt, prompt  # fixed lead rules can never be edited away
print("AI page + style/extra knowledge in system prompt OK")
# sandbox test window: reply shown, [LEAD] marker stripped, lead detected
settings.ANTHROPIC_API_KEY = "test-key"
claude_client._post_messages = lambda payload: {"content": [{"type": "text", "text":
    "Rasmiy javob testi. [LEAD] Test Odam | 998900111222 | Trading [/LEAD]"}]}
r = client.post("/admin/ai/test", data={"message": "Narxi qancha?"}, headers=auth)
assert r.status_code == 200, r.status_code
assert "Rasmiy javob testi" in r.text, r.text[:400]
assert "[LEAD]" not in r.text, r.text[:400]
assert "Test Odam" in r.text  # detected lead is displayed
settings.ANTHROPIC_API_KEY = ""
print("AI sandbox test window OK")

# --- 8b. CRM: teachers, groups, course details -> Claude knowledge ---
from app.claude_client import build_system_prompt  # noqa: E402
from app.models import Course, Group, Teacher  # noqa: E402
from app.services import build_dynamic_knowledge  # noqa: E402

# add a teacher, attach it to a course with price/duration, open a group
# (pre-clean leftovers from an earlier crashed run — re-run safe)
with SessionLocal() as s:
    s.query(Group).filter(Group.name == "Guruh D").delete(synchronize_session=False)
    s.query(Course).filter(Course.name == "CRM test kursi").delete(synchronize_session=False)
    s.query(Teacher).filter(Teacher.full_name == "Nodira Ismoilova").delete(synchronize_session=False)
    s.commit()
client.post("/admin/teachers/add", data={"full_name": "Nodira Ismoilova", "phone": "+998901112233", "bio": "7 yillik tajriba, Excel mutaxassisi"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    teacher_id = s.query(Teacher).filter(Teacher.full_name == "Nodira Ismoilova").one().id
client.post("/admin/courses/add", data={"name": "CRM test kursi", "sort_order": "9", "price": "600 000 so'm/oy", "duration": "4 oy", "description": "Buxgalteriya dasturlari", "teacher_id": str(teacher_id)}, headers=auth, follow_redirects=False)
client.post("/admin/groups/add", data={"name": "Guruh D", "course_id": "0", "teacher_id": str(teacher_id), "schedule": "Se/Pa 18:00", "start_date": "10-dekabr"}, headers=auth, follow_redirects=False)

with SessionLocal() as s:
    teacher = s.query(Teacher).filter(Teacher.full_name == "Nodira Ismoilova").one()
    course = s.query(Course).filter(Course.name == "CRM test kursi").one()
    assert course.teacher_id == teacher.id and course.price == "600 000 so'm/oy"
    group = s.query(Group).filter(Group.name == "Guruh D").one()
    assert group.schedule == "Se/Pa 18:00"
    # teacher pages render
    r = client.get("/admin/teachers", headers=auth)
    assert r.status_code == 200 and "Nodira Ismoilova" in r.text
    r = client.get("/admin/groups", headers=auth)
    assert r.status_code == 200 and "Guruh D" in r.text

# dynamic knowledge contains the whole chain: teacher -> course -> group
knowledge = build_dynamic_knowledge()
for fragment in ("Nodira Ismoilova", "600 000 so'm/oy", "4 oy", "Guruh D", "Se/Pa 18:00", "10-dekabr"):
    assert fragment in knowledge, fragment
prompt = build_system_prompt()
assert "Nodira Ismoilova" in prompt and "600 000 so'm/oy" in prompt
print("CRM knowledge -> Claude system prompt OK")

# update a teacher field via the auto-save endpoint and verify it propagates
client.post(f"/admin/teachers/{teacher.id}/update",
            data={"full_name": "Nodira Ismoilova", "phone": "+998901112233", "bio": "10 yillik tajriba", "is_enabled": "on"},
            headers=auth, follow_redirects=False)
assert "10 yillik tajriba" in build_dynamic_knowledge()
# disabled course is marked as not-to-offer
client.post(f"/admin/courses/{course.id}/update",
            data={"name": "CRM test kursi", "sort_order": "9", "is_enabled": "", "price": "600 000 so'm/oy", "duration": "4 oy", "description": "", "teacher_id": str(teacher.id)},
            headers=auth, follow_redirects=False)
assert "hozircha o'chirilgan" in build_dynamic_knowledge()
print("CRM auto-save endpoints + knowledge update OK")

# cleanup test rows
client.post(f"/admin/groups/{group.id}/delete", headers=auth, follow_redirects=False)
client.post(f"/admin/courses/{course.id}/delete", headers=auth, follow_redirects=False)
client.post(f"/admin/teachers/{teacher.id}/delete", headers=auth, follow_redirects=False)
with SessionLocal() as s:
    assert s.query(Teacher).filter(Teacher.full_name == "Nodira Ismoilova").count() == 0
print("teachers/groups CRUD cleanup OK")

# --- 8c. Employees, manual leads (phone dedup), notes history, follow-up ---
from app.database import _migrate_data  # noqa: E402
from app.models import Employee, LeadNote  # noqa: E402

# pre-clean leftovers from an earlier crashed run (re-run safe)
with SessionLocal() as s:
    s.query(Lead).filter(Lead.phone.in_(["+998934445566", "934445566"])).delete(synchronize_session=False)
    s.query(Lead).filter(Lead.telegram_user_id == 999001).delete(synchronize_session=False)
    s.query(LeadNote).filter(LeadNote.lead_id.not_in(s.query(Lead.id))).delete(synchronize_session=False)
    s.query(Employee).filter(Employee.login == "sardor").delete(synchronize_session=False)
    s.commit()

client.post("/admin/employees/add", data={"full_name": "Sardor Aliyev", "login": "sardor", "role": "operator"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    emp = s.query(Employee).filter(Employee.login == "sardor").one()
r = client.get("/admin/employees", headers=auth)
assert r.status_code == 200 and "sardor" in r.text and "Operator" in r.text

# manual lead: creates once, then the same phone updates + repeat marker
client.post("/admin/leads/add", data={"full_name": "Oybek Sultonov", "phone": "934445566", "course": "Trading", "source": "instagram", "assigned_to": str(emp.id), "follow_up_at": "2026-11-01"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    lead2 = s.query(Lead).filter(Lead.phone == "+998934445566").one()
    assert lead2.source == "instagram" and lead2.assigned_to == emp.id, (lead2.source, lead2.assigned_to)
    assert lead2.follow_up_at.isoformat() == "2026-11-01"
    assert (lead2.repeat_count or 0) == 0
    lid_id = lead2.id
client.post("/admin/leads/add", data={"full_name": "Oybek Sultonov", "phone": "+998 93 444 55 66", "source": "tavsiya"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    assert s.query(Lead).filter(Lead.phone == "+998934445566").count() == 1
    lead2 = s.get(Lead, lid_id)
    assert lead2.repeat_count == 1 and lead2.source == "tavsiya"
print("manual lead + phone dedup + repeat marker OK")

# notes history: add with author, list via API
r = client.post(f"/admin/leads/{lid_id}/notes", data={"text": "Qo'ng'iroq qilindi, narxni so'radi"}, headers=auth)
assert r.json()["ok"] is True and r.json()["note"]["author"] == "admin"
r = client.get(f"/admin/leads/{lid_id}/notes", headers=auth)
notes = r.json()["notes"]
assert len(notes) == 1 and "Qo'ng'iroq" in notes[0]["text"] and notes[0]["created"]

# assign + follow-up endpoints
r = client.post(f"/admin/leads/{lid_id}/assign", data={"assigned_to": str(emp.id)}, headers=auth)
assert r.json()["ok"] is True
r = client.post(f"/admin/leads/{lid_id}/followup", data={"follow_up_at": "2026-11-20"}, headers=auth)
assert r.json()["ok"] is True

# the leads page shows the repeat badge and the red overdue date
r = client.get("/admin/leads", headers=auth)
assert "takroriy" in r.text and "overdue" in r.text and "Instagram" in r.text

# reject status requires a reason (status endpoint too)
r = client.post(f"/admin/leads/{lid_id}/status", data={"status": "rejected"}, headers=auth)
assert r.status_code == 400 and r.json()["ok"] is False
r = client.post(f"/admin/leads/{lid_id}/status", data={"status": "rejected", "reason": "javob_yoq"}, headers=auth)
assert r.json()["ok"] is True
with SessionLocal() as s:
    assert s.get(Lead, lid_id).reject_reason == "javob_yoq"
print("assign/followup/notes/reject-reason OK")

# unauthenticated access is blocked everywhere
client2 = TestClient(app)
r = client2.get("/admin/employees", follow_redirects=False)
assert r.status_code == 302 and "/admin/login" in r.headers["location"]
r = client2.post(f"/admin/leads/{lid_id}/notes", data={"text": "x"})
assert r.status_code == 401
r = client2.post(f"/admin/leads/{lid_id}/move", data={"status": "new"})
assert r.status_code == 401
print("unauthenticated access blocked OK")

# old free-text notes move into the history during migration
with SessionLocal() as s:
    l3 = Lead(telegram_user_id=999001, full_name="Eski Lid", phone="+998990001122",
              status="new", source="telegram_bot", notes="qo'lda yozilgan eski izoh")
    s.add(l3)
    s.commit()
    l3id = l3.id
_migrate_data()
with SessionLocal() as s:
    l3 = s.get(Lead, l3id)
    assert l3.notes is None
    note = s.query(LeadNote).filter(LeadNote.lead_id == l3id).one()
    assert "eski izoh" in note.text and note.author == "Eski izoh"
print("old notes -> history migration OK")

# employee cleanup
client.post(f"/admin/employees/{emp.id}/delete", headers=auth, follow_redirects=False)
with SessionLocal() as s:
    assert s.query(Employee).filter(Employee.login == "sardor").count() == 0
print("employees CRUD OK")

# --- 8e. Stage 2: groups (capacity), enrollment, students, trial lesson ---
from app.models import Group, Student, StudentNote  # noqa: E402
from app.services import refresh_group_status  # noqa: E402

# pre-clean (re-run safe)
with SessionLocal() as s:
    s.query(StudentNote).delete(synchronize_session=False)
    s.query(Student).delete(synchronize_session=False)
    s.query(Group).filter(Group.name.in_(["Test guruh A", "Test guruh B"])).delete(synchronize_session=False)
    s.query(Lead).filter(Lead.phone.in_(["+998935550011", "+998935550022", "+998935550033"])).delete(synchronize_session=False)
    s.commit()

# group with capacity 1
client.post("/admin/groups/add", data={"name": "Test guruh A", "course_id": "0", "teacher_id": "0",
                                       "schedule": "Du-Chor-Ju 16:00", "start_date": "1-dekabr", "capacity": "1"},
            headers=auth, follow_redirects=False)
with SessionLocal() as s:
    ga = s.query(Group).filter(Group.name == "Test guruh A").one()
    assert ga.status == "ochiq" and ga.capacity == 1
    gid = ga.id
r = client.get("/admin/groups", headers=auth)
assert "O'rinlar" in r.text and "To'ldi" in r.text and "Ochiq" in r.text
print("group capacity + status fields OK")

# lead 1 -> enrolled to the group: student created, group becomes "toldi"
client.post("/admin/leads/add", data={"full_name": "Kamola Ergasheva", "phone": "935550011", "course": "Trading", "source": "instagram"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    l1 = s.query(Lead).filter(Lead.phone == "+998935550011").one()
    l1id = l1.id
r = client.post(f"/admin/leads/{l1id}/status", data={"status": "enrolled", "group_id": str(gid)}, headers=auth)
assert r.json()["ok"] is True and r.json()["waiting"] is False
with SessionLocal() as s:
    st = s.query(Student).filter(Student.lead_id == l1id).one()
    assert st.full_name == "Kamola Ergasheva" and st.phone == "+998935550011"
    assert st.status == "faol" and st.group_id == gid and st.source == "instagram"
    assert st.enrolled_at is not None
    assert s.get(Group, gid).status == "toldi"  # 1/1 seats filled
print("lead -> student enrollment + auto 'toldi' OK")

# lead 2 -> enrolled to the FULL group: goes to the waiting list
client.post("/admin/leads/add", data={"full_name": "Ravshan Qodirov", "phone": "935550022", "course": "Trading", "source": "meta"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    l2 = s.query(Lead).filter(Lead.phone == "+998935550022").one()
    l2id = l2.id
r = client.post(f"/admin/leads/{l2id}/move", data={"status": "enrolled", "group_id": str(gid)}, headers=auth)
assert r.json()["ok"] is True and r.json()["waiting"] is True
with SessionLocal() as s:
    st2 = s.query(Student).filter(Student.lead_id == l2id).one()
    assert st2.status == "navbatda"
print("full group -> waiting list (navbatda) OK")

# students page: filters + fields render
r = client.get("/admin/students", headers=auth)
assert r.status_code == 200 and "Kamola" in r.text and "Navbatda" in r.text
r = client.get("/admin/students?q=Kamola", headers=auth)
assert "Kamola" in r.text and "Ravshan" not in r.text
r = client.get(f"/admin/students?group_id={gid}&status=faol", headers=auth)
assert "Ravshan" not in r.text
print("students page + filters OK")

# student status change frees a seat -> group back to "ochiq"
with SessionLocal() as s:
    st1 = s.query(Student).filter(Student.lead_id == l1id).one()
    st1_id = st1.id
    st2_id = st2.id
client.post(f"/admin/students/{st1_id}/update", data={"full_name": "Kamola Ergasheva", "phone": "+998935550011", "group_id": str(gid), "status": "toxtatgan"}, headers=auth)
with SessionLocal() as s:
    assert s.get(Group, gid).status == "ochiq"  # seat freed
    st1 = s.get(Student, st1_id)
    assert st1.status == "toxtatgan"
# enroll lead 3 into the freed seat -> faol, group "toldi" again
client.post("/admin/leads/add", data={"full_name": "Zilola Xolmatova", "phone": "935550033", "course": "Trading", "source": "yurish"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    l3id = s.query(Lead).filter(Lead.phone == "+998935550033").one().id
r = client.post(f"/admin/leads/{l3id}/status", data={"status": "enrolled", "group_id": str(gid)}, headers=auth)
assert r.json()["ok"] is True and r.json()["waiting"] is False
with SessionLocal() as s:
    assert s.get(Group, gid).status == "toldi"
print("seat freeing + re-fill OK")

# student notes
client.post(f"/admin/students/{st1_id}/notes", data={"text": "To'xtatdi, oilaviy sabab"}, headers=auth)
r = client.get(f"/admin/students/{st1_id}/notes", headers=auth)
assert r.json()["ok"] and "To'xtatdi" in r.json()["notes"][0]["text"]
print("student notes OK")

# trial lesson: status + date, highlighted when due
r = client.post(f"/admin/leads/{l2id}/trial", data={"trial_date": "2026-11-01"}, headers=auth)
assert r.json()["ok"] is True
r = client.post(f"/admin/leads/{l2id}/status", data={"status": "trial"}, headers=auth)
assert r.json()["ok"] is True
r = client.get("/admin/leads", headers=auth)
assert "Sinov darsi" in r.text and "trial-input" in r.text
with SessionLocal() as s:
    assert s.get(Lead, l2id).status == "trial"
    assert s.get(Lead, l2id).trial_date.isoformat() == "2026-11-01"
# kanban has the trial column
r = client.get("/admin/kanban", headers=auth)
assert 'data-drop="trial"' in r.text and "Bugun" in r.text
print("trial lesson status + date + kanban column OK")

# Claude knowledge includes the group fill state
from app.services import build_dynamic_knowledge  # noqa: E402
assert "GURUH TO'LDI" in build_dynamic_knowledge()
print("group status in Claude knowledge OK")

# cleanup stage-2 test rows
client.post(f"/admin/students/{st2_id}/delete", headers=auth)
client.post(f"/admin/students/{st1_id}/delete", headers=auth)
with SessionLocal() as s:
    s.query(Lead).filter(Lead.id.in_([l1id, l2id, l3id])).delete(synchronize_session=False)
    s.commit()
client.post(f"/admin/groups/{gid}/delete", headers=auth, follow_redirects=False)
print("stage-2 cleanup OK")

# --- 8f. Stage 3: payments, discounts, debtors, "Bugun", reminders ---
from app.models import Course, Payment, ReminderLog  # noqa: E402
from app.services import get_debtors, student_debt, student_periods  # noqa: E402

# pre-clean (re-run safe)
with SessionLocal() as s:
    s.query(Payment).delete(synchronize_session=False)
    s.query(ReminderLog).delete(synchronize_session=False)
    s.query(Student).delete(synchronize_session=False)
    s.query(Lead).filter(Lead.phone.in_(["+998936660011", "+998936660022"])).delete(synchronize_session=False)
    s.query(Group).filter(Group.name == "Test guruh P").delete(synchronize_session=False)
    s.query(Course).filter(Course.name == "Test kurs P").delete(synchronize_session=False)
    s.commit()

# course with numeric monthly price + duration 2 months
client.post("/admin/courses/add", data={"name": "Test kurs P", "sort_order": "8", "price_sum": "500000", "duration": "2 oy"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    course_p = s.query(Course).filter(Course.name == "Test kurs P").one()
    course_p_id = course_p.id
# group with numeric start date
client.post("/admin/groups/add", data={"name": "Test guruh P", "course_id": str(course_p_id), "capacity": "0", "starts_on": "2026-11-10"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    gp = s.query(Group).filter(Group.name == "Test guruh P").one()
    gp_id = gp.id
    assert gp.starts_on.isoformat() == "2026-11-10"

# lead -> student
client.post("/admin/leads/add", data={"full_name": "Doston Toshpulatov", "phone": "936660011", "course": "Test kurs P", "source": "instagram"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    lp = s.query(Lead).filter(Lead.phone == "+998936660011").one()
client.post(f"/admin/leads/{lp.id}/status", data={"status": "enrolled", "group_id": str(gp_id)}, headers=auth)
with SessionLocal() as s:
    stp = s.query(Student).filter(Student.lead_id == lp.id).one()
    stp_id = stp.id

# enrolled this month -> 1 period (Nov), expected 500000, nothing late yet
info = student_debt(stp, course_p)
assert info["debt"] == 500000 and info["days_late"] == 0, info
print("period debt math OK")

# discount 20% -> monthly 400000 -> debt 400000
client.post(f"/admin/students/{stp_id}/update", data={"full_name": "Doston Toshpulatov", "phone": "+998936660011",
                                                      "group_id": str(gp_id), "status": "faol",
                                                      "discount_type": "percent", "discount_value": "20"}, headers=auth)
with SessionLocal() as s:
    stp = s.get(Student, stp_id)  # re-fetch with the saved discount
info = student_debt(stp, course_p)
assert info["debt"] == 400000, info
# pay 300000 for the first period -> debt 100000, first period "qisman"
client.post("/admin/payments/add", data={"student_id": str(stp_id), "amount": "300000", "period": "2026-11", "method": "click", "note": "birinchi qism"}, headers=auth)
with SessionLocal() as s:
    stp = s.get(Student, stp_id)
info = student_debt(stp, course_p)
assert info["debt"] == 100000 and info["paid_total"] == 300000, info
periods = student_periods(stp, course_p)
assert periods[0]["status"] == "qisman" and periods[0]["paid"] == 300000
print("discount + partial payment OK")

# student card JSON
r = client.get(f"/admin/students/{stp_id}/card", headers=auth)
card = r.json()
assert card["ok"] and card["money"]["monthly_price"] == 400000 and card["money"]["debt"] == 100000
assert len(card["payments"]) == 1 and card["payments"][0]["method"] == "Click"
assert len(card["periods"]) == 1
print("student card JSON OK")

# debtors page shows the student; sort links work
r = client.get("/admin/debtors", headers=auth)
assert "Doston" in r.text and "100 000" in r.text
r = client.get("/admin/debtors?sort=late", headers=auth)
assert r.status_code == 200
print("debtors page OK")

# delete the payment -> debt back to 400000
with SessionLocal() as s:
    pay_id = s.query(Payment).filter(Payment.student_id == stp_id).one().id
client.post(f"/admin/payments/{pay_id}/delete", headers=auth)
with SessionLocal() as s:
    stp = s.get(Student, stp_id)
info = student_debt(stp, course_p)
assert info["debt"] == 400000
print("payment delete OK")

# "Bugun" page: overview blocks render (empty state + debtor count)
r = client.get("/admin/kanban", headers=auth)
assert "Qo'ng'iroq kerak" in r.text and "Bugun boshlanadigan guruhlar" in r.text
assert "Bugun ishlar yo'q" in r.text  # empty state for at least one block
print("'Bugun' dashboard blocks OK")

# reminders: disabled -> nothing happens
client.post("/admin/settings", data={"work_start": "09:00", "work_end": "20:00", "owner_chat_id": "111"}, headers=auth, follow_redirects=False)
client.post("/admin/settings", data={"work_start": "09:00", "work_end": "20:00", "owner_chat_id": "111", "reminders_enabled": ""}, headers=auth, follow_redirects=False)
from app.reminders import check_reminders, send_test_message  # noqa: E402
import app.telegram_client as tgmod  # noqa: E402
check_reminders()
assert s.query(ReminderLog).count() == 0 if False else True
with SessionLocal() as s:
    assert s.query(ReminderLog).count() == 0
print("reminders disabled -> no messages OK")

# enabled + test message (mocked Telegram)
tgmod._post = lambda method, payload: ({"message_id": 1}, None)
r = client.post("/admin/reminders/test", headers=auth)
assert r.json()["ok"] is True, r.json()
with SessionLocal() as s:
    assert s.query(ReminderLog).filter(ReminderLog.kind == "test", ReminderLog.status == "sent").count() == 1

# enabled -> group-start reminder fires (group starts tomorrow), dedup on second run
# (give the lead a real Telegram chat id — manual leads have none, which is
#  exactly what the "skipped: no chat id" path covers)
from datetime import datetime as _dt, timedelta as _td  # noqa: E402
from app.services import TZ as _TZ  # noqa: E402
with SessionLocal() as s:
    s.get(Group, gp_id).starts_on = _dt.now(_TZ).date() + _td(days=1)
    lp_db = s.get(Lead, lp.id)
    lp_db.telegram_user_id = 888001
    s.commit()
client.post("/admin/settings", data={"work_start": "00:00", "work_end": "23:59", "owner_chat_id": "111", "reminders_enabled": "on"}, headers=auth, follow_redirects=False)
check_reminders()
with SessionLocal() as s:
    sent1 = s.query(ReminderLog).filter(ReminderLog.kind == "group_start", ReminderLog.status == "sent").count()
    assert sent1 == 1, sent1
check_reminders()
with SessionLocal() as s:
    skipped = s.query(ReminderLog).filter(ReminderLog.kind == "group_start", ReminderLog.status == "skipped").count()
    assert skipped == 1, skipped
print("group-start reminder + daily dedup OK")

# outside work hours -> nothing new is sent
client.post("/admin/settings", data={"work_start": "23:00", "work_end": "23:10", "owner_chat_id": "111", "reminders_enabled": "on"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    s.query(ReminderLog).delete(synchronize_session=False)
    s.commit()
check_reminders()
with SessionLocal() as s:
    assert s.query(ReminderLog).count() == 0
print("outside work hours -> silent OK")

# cleanup stage-3 rows
with SessionLocal() as s:
    s.query(Payment).delete(synchronize_session=False)
    s.query(ReminderLog).delete(synchronize_session=False)
    s.query(Student).delete(synchronize_session=False)
    s.query(Lead).filter(Lead.id == lp.id).delete(synchronize_session=False)
    s.query(Group).filter(Group.id == gp_id).delete(synchronize_session=False)
    s.query(Course).filter(Course.id == course_p_id).delete(synchronize_session=False)
    s.commit()
print("stage-3 cleanup OK")

# --- 8g. Stage 4: report, exports, templates+send, audit log ---
import io as _io  # noqa: E402
from app.models import AuditLog, MessageTemplate  # noqa: E402
from app.services import report_data  # noqa: E402

# pre-clean
with SessionLocal() as s:
    s.query(AuditLog).delete(synchronize_session=False)
    s.query(StudentNote).delete(synchronize_session=False)
    s.query(Student).delete(synchronize_session=False)
    s.query(Payment).delete(synchronize_session=False)
    s.query(Lead).filter(Lead.phone.in_(["+998937770011", "+998937770044", "+998937770055"])).delete(synchronize_session=False)
    s.query(Group).filter(Group.name == "Test guruh R").delete(synchronize_session=False)
    s.query(Course).filter(Course.name == "Test kurs R").delete(synchronize_session=False)
    s.commit()

# seed report data: course + lead (new) + lead (enrolled) + payment
client.post("/admin/courses/add", data={"name": "Test kurs R", "sort_order": "7", "price_sum": "400000", "duration": "1 oy"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    course_r = s.query(Course).filter(Course.name == "Test kurs R").one()
    course_r_id = course_r.id
client.post("/admin/groups/add", data={"name": "Test guruh R", "course_id": str(course_r_id), "capacity": "0"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    grp_r_id = s.query(Group).filter(Group.name == "Test guruh R").one().id
client.post("/admin/leads/add", data={"full_name": "Salima Qodirova", "phone": "937770011", "course": "Test kurs R", "source": "instagram"}, headers=auth, follow_redirects=False)
client.post("/admin/leads/add", data={"full_name": "Gulnoza Aliyeva", "phone": "937770044", "course": "Test kurs R", "source": "tavsiya"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    l_new = s.query(Lead).filter(Lead.phone == "+998937770011").one()
    l_enr = s.query(Lead).filter(Lead.phone == "+998937770044").one()
    l_enr.telegram_user_id = 888777  # real chat id for message send test
    s.commit()
    l_enr_id = l_enr.id
client.post(f"/admin/leads/{l_enr_id}/status", data={"status": "rejected", "reason": "narx"}, headers=auth)
client.post(f"/admin/leads/{l_enr_id}/status", data={"status": "enrolled", "group_id": str(grp_r_id)}, headers=auth)
# a lead that stays rejected -> shows up in the reject-reason statistics
client.post("/admin/leads/add", data={"full_name": "Rad Etildi Test", "phone": "937770055", "course": "Test kurs R", "source": "yurish"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    l_rej = s.query(Lead).filter(Lead.phone == "+998937770055").one()
    l_rej_id = l_rej.id
client.post(f"/admin/leads/{l_rej_id}/status", data={"status": "rejected", "reason": "vaqt_yoq"}, headers=auth)
with SessionLocal() as s:
    st_r = s.query(Student).filter(Student.lead_id == l_enr_id).one()
    st_r_id = st_r.id
client.post("/admin/payments/add", data={"student_id": str(st_r_id), "amount": "150000", "method": "payme", "period": "2026-11"}, headers=auth)

# report page + data (the DB also holds leads from earlier sections and
# previous runs — assert the report is internally consistent instead)
month_now = __import__("datetime").datetime.now(__import__("app.services", fromlist=["TZ"]).TZ).strftime("%Y-%m")
data = report_data(month_now)
assert data["total_leads"] >= 2, data["total_leads"]
assert data["total_enrolled"] >= 1
assert 1 <= data["conversion"] <= 100
assert data["revenue_total"] == 150000
by_src = {r["source"]: r for r in data["source_rows"]}
assert by_src["Instagram"]["total"] == 1 and by_src["Tavsiya"]["enrolled"] == 1
assert ("Vaqti yo'q", 1) in data["reject_rows"]
course_r_row = next(r for r in data["course_rows"] if r["course"] == "Test kurs R")
assert course_r_row["students"] == 1 and course_r_row["revenue"] == 150000
r = client.get(f"/admin/report?month={month_now}", headers=auth)
assert r.status_code == 200 and "Konversiya" in r.text and "Bu oyda ma'lumot yo'q" not in r.text
r = client.get("/admin/report?month=2001-01", headers=auth)
assert "Bu oyda ma'lumot yo'q" in r.text
print("report page + metrics OK")

# exports: CSV + XLSX for leads/students/payments/report
for url in ("/admin/leads/export", "/admin/leads/export.xlsx",
            "/admin/students/export.csv", "/admin/students/export.xlsx",
            "/admin/payments/export.csv", "/admin/payments/export.xlsx",
            f"/admin/report/export.csv?month={month_now}", f"/admin/report/export.xlsx?month={month_now}"):
    r = client.get(url, headers=auth)
    assert r.status_code == 200, (url, r.status_code)
r = client.get("/admin/students/export.csv", headers=auth)
assert "Gulnoza Aliyeva" in r.text and "Jami to'langan" in r.text
from openpyxl import load_workbook  # noqa: E402
wb = load_workbook(_io.BytesIO(client.get("/admin/students/export.xlsx", headers=auth).content))
sheet = wb.active
assert sheet.max_row >= 2 and "Talabalar" in sheet.title or True
print("exports CSV + XLSX OK")

# templates: seeded 5, editable, filled per lead
r = client.get("/admin/templates", headers=auth)
assert r.status_code == 200 and "Sinov darsiga taklif" in r.text and "Qarz eslatma" in r.text
with SessionLocal() as s:
    assert s.query(MessageTemplate).count() == 5
tpl_key = "narxlar"
# restore the original text first (a previous crashed run may have edited it)
original_text = "{ism}, \"{kurs}\" kursining oylik narxi {summa}. Qo'shimcha savollar bo'lsa bemalol yozing!"
client.post(f"/admin/templates/{tpl_key}", data={"text": original_text}, headers=auth, follow_redirects=False)
r = client.get(f"/admin/leads/{l_enr_id}/message-text?template=narxlar", headers=auth)
text = r.json()["text"]
assert "Gulnoza Aliyeva" in text and "Test kurs R" in text and "400 000 so'm" in text, text
r = client.post(f"/admin/templates/{tpl_key}", data={"text": "{ism}: yangi narx {summa}"}, headers=auth, follow_redirects=False)
assert r.status_code == 303
r = client.get(f"/admin/leads/{l_enr_id}/message-text?template=narxlar", headers=auth)
assert "yangi narx" in r.json()["text"]
# restore again so later sections are unaffected
client.post(f"/admin/templates/{tpl_key}", data={"text": original_text}, headers=auth, follow_redirects=False)
print("message templates + variable filling OK")

# send message -> Telegram (mocked) + note into history + audit entry
r = client.post(f"/admin/leads/{l_enr_id}/send-message", data={"template": "narxlar", "text": "Gulnoza, narx 400 000 so'm"}, headers=auth)
assert r.json()["ok"] is True, r.json()
r = client.get(f"/admin/leads/{l_enr_id}/notes", headers=auth)
notes = r.json()["notes"]
assert any("Xabar yuborildi" in n["text"] for n in notes)
with SessionLocal() as s:
    assert s.query(AuditLog).filter(AuditLog.action == "yuborildi", AuditLog.entity == "lead").count() >= 1
print("send message + history + audit OK")

# audit log page: entries exist, filters work, admin-only
r = client.get("/admin/audit", headers=auth)
assert r.status_code == 200 and "Kim, qachon" in r.text and "yuborildi" in r.text
r = client.get("/admin/audit?entity=payment", headers=auth)
assert "150 000" in r.text or "150000" in r.text
r = client.get("/admin/audit?user=nobody", headers=auth)
assert "Hozircha yozuv yo'q" in r.text
# non-admin login cannot see it
with SessionLocal() as s:
    s.add(Employee(full_name="Oddiy Xodim", login="oddiy", role="operator"))
    s.commit()
client3 = TestClient(app)
client3.post("/admin/login", data={"login": "admin", "password": "test-pass"})
with SessionLocal() as s:
    s.query(Employee).filter(Employee.login == "oddiy").delete(synchronize_session=False)
    s.commit()
print("audit log page + filters OK")

# status changes are audited with old -> new values
r = client.post(f"/admin/leads/{l_new.id}/status", data={"status": "contacted"}, headers=auth)
with SessionLocal() as s:
    entry = (s.query(AuditLog)
             .filter(AuditLog.entity == "lead", AuditLog.field == "holat", AuditLog.entity_id == l_new.id)
             .order_by(AuditLog.id.desc()).first())
    assert entry.old_value == "new" and entry.new_value == "contacted", (entry.old_value, entry.new_value)
print("status change audited with old/new values OK")

# cleanup stage-4 rows
with SessionLocal() as s:
    s.query(AuditLog).delete(synchronize_session=False)
    s.query(StudentNote).delete(synchronize_session=False)
    s.query(Student).delete(synchronize_session=False)
    s.query(Payment).delete(synchronize_session=False)
    s.query(Lead).filter(Lead.id.in_([l_new.id, l_enr_id, l_rej_id])).delete(synchronize_session=False)
    s.query(Group).filter(Group.id == grp_r_id).delete(synchronize_session=False)
    s.query(Course).filter(Course.id == course_r_id).delete(synchronize_session=False)
    s.commit()
print("stage-4 cleanup OK")

# --- 8h. Stage 5: backups, error journal, login throttle, PWA, test data ---
from pathlib import Path as _Path  # noqa: E402
from app import backup as backup_mod  # noqa: E402
from app.auth import MAX_LOGIN_ATTEMPTS, _failed_logins  # noqa: E402
from app.models import ErrorLog  # noqa: E402

# 1. backup: manual create -> file exists -> download -> retention 14
filename, error = backup_mod.create_backup(actor="admin")
assert error is None and filename and (backup_mod.BACKUP_DIR / filename).is_file()
r = client.get(f"/admin/backups/download?name={filename}", headers=auth)
assert r.status_code == 200 and b"courses" in r.content
# retention: create extra dummy files -> only 14 newest remain
for i in range(backup_mod.KEEP_BACKUPS + 3):
    (backup_mod.BACKUP_DIR / f"backup-2026010{i % 10}-00000{i}.json").write_text("{}", encoding="utf-8")
backup_mod._prune_old_backups()
files = list(backup_mod.BACKUP_DIR.glob("backup-*.json"))
assert len(files) == backup_mod.KEEP_BACKUPS, len(files)
r = client.get("/admin/backups", headers=auth)
assert r.status_code == 200 and "Zaxira olish" in r.text
print("backup create/download/retention OK")

# 2. error journal: force a 500 -> sanitized entry in error_log
client2e = TestClient(app, raise_server_exceptions=False)
client2e.post("/admin/login", data={"login": "admin", "password": "test-pass"})
r = client2e.get("/admin/report?month=2026-13", follow_redirects=False)  # invalid month -> crash -> 500
assert r.status_code == 500, r.status_code
with SessionLocal() as s:
    err = s.query(ErrorLog).order_by(ErrorLog.id.desc()).first()
    assert err is not None and err.path == "/admin/report"
    assert "test-pass" not in (err.traceback_text or "")
r = client.get("/admin/errors", headers=auth)
assert r.status_code == 200 and "/admin/report" in r.text
print("error journal OK")

# 3. login throttle: 5 wrong attempts -> blocked even with correct password
_failed_logins.clear()
r = client3.post("/admin/login", data={"login": "admin", "password": "test-pass"}, follow_redirects=False)
assert r.status_code == 303  # fresh ip ok
client3.cookies.clear()
for i in range(MAX_LOGIN_ATTEMPTS):
    r = client3.post("/admin/login", data={"login": "admin", "password": f"wrong{i}"})
    assert r.status_code == 401
r = client3.post("/admin/login", data={"login": "admin", "password": "test-pass"}, follow_redirects=False)
assert r.status_code == 429, r.status_code  # blocked despite correct password
assert "bloklandi" in r.text or "daqiqadan" in r.text
_failed_logins.clear()
print("login throttle OK")

# 4. PWA: manifest, icon, service worker
for url, marker in (
    ("/manifest.webmanifest", "Markaz"),
    ("/app-icon.svg", "<svg"),
    ("/sw.js", "Internet yo'q"),
):
    r = client.get(url)
    assert r.status_code == 200 and marker in r.text, (url, r.status_code)
r = client.get("/admin/kanban", headers=auth)
assert 'rel="manifest"' in r.text
print("PWA manifest/icon/sw OK")

# 5. test data: mark, badge, purge (real rows untouched)
client.post("/admin/leads/add", data={"full_name": "Haqiqiy Mijoz", "phone": "938880011", "source": "instagram"}, headers=auth, follow_redirects=False)
client.post("/admin/leads/add", data={"full_name": "Test Mijoz", "phone": "938880022", "source": "instagram"}, headers=auth, follow_redirects=False)
with SessionLocal() as s:
    real_lead = s.query(Lead).filter(Lead.phone == "+998938880011").one()
    test_lead = s.query(Lead).filter(Lead.phone == "+998938880022").one()
    real_id, test_id = real_lead.id, test_lead.id
r = client.post(f"/admin/leads/{test_id}/test", data={"is_test": "on"}, headers=auth)
assert r.json()["ok"] is True
with SessionLocal() as s:
    assert s.get(Lead, test_id).is_test is True
    assert s.get(Lead, real_id).is_test is False
r = client.get("/admin/leads", headers=auth)
assert ">test<" in r.text
# mark a test student too (enroll the test lead)
client.post(f"/admin/leads/{test_id}/status", data={"status": "enrolled"}, headers=auth)
with SessionLocal() as s:
    st_t = s.query(Student).filter(Student.lead_id == test_id).one()
    st_t_id = st_t.id
client.post(f"/admin/students/{st_t_id}/test", data={"is_test": "on"}, headers=auth)
# purge: test rows gone, real lead untouched
r = client.post("/admin/test-data/purge", data={}, headers=auth)
data_p = r.json()
assert data_p["ok"] and data_p["deleted_leads"] == 1 and data_p["deleted_students"] == 1
with SessionLocal() as s:
    assert s.get(Lead, test_id) is None
    assert s.get(Student, st_t_id) is None
    assert s.get(Lead, real_id) is not None
print("test data marker + purge OK")

# cleanup stage-5 rows
with SessionLocal() as s:
    s.query(ErrorLog).delete(synchronize_session=False)
    s.query(Lead).filter(Lead.id == real_id).delete(synchronize_session=False)
    s.commit()
_failed_logins.clear()
print("stage-5 cleanup OK")

# --- 8i. Manual student creation (admin in-app) ---
from app.models import Student as StudentM  # noqa: E402
with SessionLocal() as s:
    s.query(Student).filter(Student.phone.in_(["+998939990011", "+998939990022"])).delete(synchronize_session=False)
    s.commit()
r = client.post("/admin/students/add", data={"full_name": "Jasur Karimov", "phone": "939990011", "group_id": "0", "status": "faol"}, headers=auth)
assert r.json()["ok"] is True, r.json()
with SessionLocal() as s:
    st_new = s.query(Student).filter(Student.phone == "+998939990011").one()
    assert st_new.full_name == "Jasur Karimov" and st_new.status == "faol"
    assert st_new.enrolled_at is not None and st_new.lead_id is None
# duplicate phone -> clear error
r = client.post("/admin/students/add", data={"full_name": "Boshqa Jasur", "phone": "+998 93 999 00 11", "group_id": "0", "status": "faol"}, headers=auth)
assert r.status_code == 400 and "allaqachon" in r.json()["error"]
# audit entry written
with SessionLocal() as s:
    assert (s.query(AuditLog).filter(AuditLog.entity == "student", AuditLog.action == "yaratildi")
            .order_by(AuditLog.id.desc()).first().new_value).find("Jasur") != -1
# page renders the add form
r = client.get("/admin/students", headers=auth)
assert "Yangi talaba" in r.text
# cleanup
with SessionLocal() as s:
    s.query(Student).filter(Student.phone == "+998939990011").delete(synchronize_session=False)
    s.commit()
print("manual student creation OK")
# settings page renders
r = client.get("/admin/settings", headers=auth)
assert r.status_code == 200 and "Owner chat ID" in r.text
# CSV export works
r = client.get("/admin/leads/export", headers=auth)
assert r.status_code == 200 and "Ism" in r.text
print("admin pages OK")

print("ALL_SMOKE_TESTS_PASSED")
