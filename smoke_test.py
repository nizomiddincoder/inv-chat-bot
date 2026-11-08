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
assert r.status_code == 200 and r.json() == {"ok": True, "lead_id": lead_id, "status": target}
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
# settings page renders
r = client.get("/admin/settings", headers=auth)
assert r.status_code == 200 and "Owner chat ID" in r.text
# CSV export works
r = client.get("/admin/leads/export", headers=auth)
assert r.status_code == 200 and "Ism" in r.text
print("admin pages OK")

print("ALL_SMOKE_TESTS_PASSED")
