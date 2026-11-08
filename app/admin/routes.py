"""Admin panel routes (/admin) protected by HTTP Basic Auth."""
import base64
import csv
import io
import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..database import SessionLocal
from ..models import BotMessage, Course, Lead
from ..services import (
    DEFAULT_MESSAGES,
    TZ,
    get_setting,
    get_work_hours,
    set_setting,
)

log = logging.getLogger("admin")

router = APIRouter(prefix="/admin")

_templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=_templates_dir)


def _format_tashkent(dt: datetime | None) -> str:
    """Show times in Tashkent local time in the admin tables."""
    if dt is None:
        return ""
    if dt.tzinfo is None:  # sqlite stores naive datetimes
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Asia/Tashkent")).strftime("%d.%m.%Y %H:%M")


templates.env.filters["tashkent"] = _format_tashkent

STATUS_LABELS = {
    "new": "Yangi",
    "contacted": "Aloqaga chiqildi",
    "enrolled": "O'qishga yozildi",
    "rejected": "Rad etildi",
}

MESSAGE_LABELS = {
    "welcome": "1. Salomlashish (birinchi javob) — {first_name} ishlatiladi",
    "after_course": "2. Kurs tanlangandan keyin — {first_name}, {course} ishlatiladi",
    "after_name": "3. Ism qabul qilingandan keyin (telefon so'raladi)",
    "after_phone": "4. Telefon noto'g'ri kiritilsa",
    "name_invalid": "5. Ism noto'g'ri kiritilsa",
    "name_edited": "6. Ism tahrirlangandagi tasdiq (xabar o'zgartirilsa)",
    "finish": "7. Yakuniy xabar (lid qabul qilindi) — {first_name} ishlatiladi",
    "out_of_hours": "8. Ish vaqtidan tashqari qo'shiladigan satr",
}


# ---------------------------------------------------------------------------
# Basic Auth
# ---------------------------------------------------------------------------

def check_auth(authorization: str | None = Header(default=None)) -> None:
    """HTTP Basic Auth with ADMIN_USER / ADMIN_PASSWORD."""
    if not settings.ADMIN_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PASSWORD env variable is not set. Admin panel is disabled.",
        )
    expected = "Basic " + base64.b64encode(
        f"{settings.ADMIN_USER}:{settings.ADMIN_PASSWORD}".encode("utf-8")
    ).decode("ascii")
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Inventor Admin"'},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _filtered_leads(course: str, status: str, q: str) -> list[Lead]:
    with SessionLocal() as session:
        query = session.query(Lead)
        if course:
            query = query.filter(Lead.course == course)
        if status:
            query = query.filter(Lead.status == status)
        if q:
            like = f"%{q.strip()}%"
            query = query.filter((Lead.full_name.ilike(like)) | (Lead.phone.ilike(like)))
        return query.order_by(Lead.created_at.desc()).limit(500).all()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("")
@router.get("/")
def index(_: None = Depends(check_auth)) -> RedirectResponse:
    return RedirectResponse(url="/admin/leads", status_code=302)


@router.get("/leads")
def leads_page(
    request: Request,
    course: str = "",
    status: str = "",
    q: str = "",
    saved: int = 0,
    _: None = Depends(check_auth),
):
    leads_list = _filtered_leads(course, status, q)
    with SessionLocal() as session:
        course_names = [
            c.name for c in session.query(Course).order_by(Course.sort_order).all()
        ]
    query_string = urlencode({"course": course, "status": status, "q": q})
    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            "leads": leads_list,
            "course_names": course_names,
            "status_labels": STATUS_LABELS,
            "f_course": course,
            "f_status": status,
            "f_q": q,
            "query_string": query_string,
            "saved": saved,
            "active": "leads",
        },
    )


@router.post("/leads/{lead_id}/status")
def update_lead_status(
    lead_id: int,
    status: str = Form(...),
    _: None = Depends(check_auth),
):
    if status not in STATUS_LABELS:
        raise HTTPException(status_code=400, detail="Unknown status")
    with SessionLocal() as session:
        lead = session.get(Lead, lead_id)
        if lead:
            lead.status = status
            session.commit()
            log.info("Lead %s status -> %s", lead_id, status)
    return RedirectResponse(url="/admin/leads?saved=1", status_code=303)


@router.post("/leads/{lead_id}/notes")
def update_lead_notes(
    lead_id: int,
    notes: str = Form(""),
    _: None = Depends(check_auth),
):
    with SessionLocal() as session:
        lead = session.get(Lead, lead_id)
        if lead:
            lead.notes = notes.strip() or None
            session.commit()
    return RedirectResponse(url="/admin/leads?saved=1", status_code=303)


@router.get("/leads/export")
def export_csv(course: str = "", status: str = "", q: str = "", _: None = Depends(check_auth)):
    """Download filtered leads as CSV (opens in Excel)."""
    leads_list = _filtered_leads(course, status, q)
    buffer = io.StringIO()
    buffer.write("\ufeff")  # BOM so Excel shows Uzbek letters correctly
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["ID", "Ism", "Telefon", "Kurs", "Username", "Holat", "Izoh", "Yaratildi", "Yangilandi"])
    for lead in leads_list:
        writer.writerow([
            lead.id,
            lead.full_name or "",
            lead.phone or "",
            lead.course or "",
            lead.username or "",
            STATUS_LABELS.get(lead.status, lead.status),
            lead.notes or "",
            _format_tashkent(lead.created_at),
            _format_tashkent(lead.updated_at),
        ])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


# ---------------------------------------------------------------------------
# Courses
# ---------------------------------------------------------------------------

@router.get("/courses")
def courses_page(request: Request, saved: int = 0, _: None = Depends(check_auth)):
    with SessionLocal() as session:
        courses = session.query(Course).order_by(Course.sort_order, Course.id).all()
    return templates.TemplateResponse(
        request,
        "courses.html",
        {"courses": courses, "saved": saved, "active": "courses"},
    )


@router.post("/courses/add")
def add_course(
    name: str = Form(...),
    sort_order: int = Form(0),
    _: None = Depends(check_auth),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Course name is required")
    with SessionLocal() as session:
        exists = session.query(Course).filter(Course.name == name).first()
        if not exists:
            session.add(Course(name=name, sort_order=sort_order, is_enabled=True))
            session.commit()
            log.info("Course added: %s", name)
    return RedirectResponse(url="/admin/courses?saved=1", status_code=303)


@router.post("/courses/{course_id}/update")
def update_course(
    course_id: int,
    name: str = Form(...),
    sort_order: int = Form(0),
    is_enabled: str = Form(""),
    _: None = Depends(check_auth),
):
    with SessionLocal() as session:
        course = session.get(Course, course_id)
        if course:
            course.name = name.strip() or course.name
            course.sort_order = sort_order
            course.is_enabled = is_enabled == "on"  # checkbox: present = "on"
            session.commit()
            log.info("Course updated: id=%s name=%s enabled=%s", course_id, course.name, course.is_enabled)
    return RedirectResponse(url="/admin/courses?saved=1", status_code=303)


@router.post("/courses/{course_id}/delete")
def delete_course(course_id: int, _: None = Depends(check_auth)):
    with SessionLocal() as session:
        course = session.get(Course, course_id)
        if course:
            session.delete(course)
            session.commit()
            log.info("Course deleted: id=%s name=%s", course_id, course.name)
    return RedirectResponse(url="/admin/courses?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Messages (bot text templates)
# ---------------------------------------------------------------------------

@router.get("/messages")
def messages_page(request: Request, saved: int = 0, _: None = Depends(check_auth)):
    with SessionLocal() as session:
        rows = {m.key: m.text for m in session.query(BotMessage).all()}
    # Show every known template even if it is missing from the DB.
    messages = [
        {"key": key, "label": MESSAGE_LABELS.get(key, key), "text": rows.get(key, DEFAULT_MESSAGES.get(key, ""))}
        for key in DEFAULT_MESSAGES
    ]
    return templates.TemplateResponse(
        request,
        "messages.html",
        {"messages": messages, "saved": saved, "active": "messages"},
    )


@router.post("/messages/{key}")
def update_message(key: str, text: str = Form(...), _: None = Depends(check_auth)):
    if key not in DEFAULT_MESSAGES:
        raise HTTPException(status_code=400, detail="Unknown message key")
    with SessionLocal() as session:
        row = session.get(BotMessage, key)
        if row is None:
            session.add(BotMessage(key=key, text=text))
        else:
            row.text = text
        session.commit()
    log.info("Message template updated: %s", key)
    return RedirectResponse(url="/admin/messages?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@router.get("/settings")
def settings_page(request: Request, saved: int = 0, _: None = Depends(check_auth)):
    start, end = get_work_hours()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "work_start": get_setting("work_start", start.strftime("%H:%M")),
            "work_end": get_setting("work_end", end.strftime("%H:%M")),
            "owner_chat_id": get_setting("owner_chat_id", ""),
            "saved": saved,
            "active": "settings",
            "tz": str(TZ),
        },
    )


@router.post("/settings")
def save_settings(
    work_start: str = Form(...),
    work_end: str = Form(...),
    owner_chat_id: str = Form(""),
    _: None = Depends(check_auth),
):
    # Validate the times before saving.
    for value, label in ((work_start, "WORK_START"), (work_end, "WORK_END")):
        try:
            datetime.strptime(value.strip(), "%H:%M")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{label} format is HH:MM")
    set_setting("work_start", work_start.strip())
    set_setting("work_end", work_end.strip())
    owner = owner_chat_id.strip()
    if owner and not owner.lstrip("-").isdigit():
        raise HTTPException(status_code=400, detail="Owner chat id must be a number")
    set_setting("owner_chat_id", owner)
    log.info("Settings saved: work %s-%s, owner=%s", work_start, work_end, owner or "-")
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)
