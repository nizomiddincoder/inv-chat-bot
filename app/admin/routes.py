"""Admin panel routes (/admin) protected by a session cookie login."""
import csv
import io
import logging
import os
from datetime import date, datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import func

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import claude_client
from ..auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    check_credentials,
    create_session,
    verify_session,
)
from ..config import settings
from ..database import SessionLocal
from .. import reminders
from ..models import Course, Group, Lead, Payment, ReminderLog, Student, StudentNote, Teacher
from ..services import (
    AI_KNOWLEDGE_KEY,
    AI_STYLE_KEY,
    GROUP_STATUSES,
    PAYMENT_METHODS,
    REJECT_REASONS,
    ROLES,
    SOURCES,
    STUDENT_STATUSES,
    TZ,
    add_lead_note,
    add_payment,
    add_student_note,
    enroll_lead,
    get_ai_text,
    get_all_courses,
    get_debtors,
    get_employees,
    get_lead_notes,
    get_setting,
    get_student_notes,
    get_students_filtered,
    get_teachers,
    get_work_hours,
    monthly_price,
    normalize_phone,
    set_ai_text,
    set_setting,
    set_student_status,
    source_label,
    student_debt,
    student_periods,
    today_overview,
    upsert_manual_lead,
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


def _format_sum(amount) -> str:
    """Whole so'm with thin space separators, e.g. 500 000."""
    try:
        return f"{int(amount or 0):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(amount or 0)


templates.env.filters["som"] = _format_sum
templates.env.globals["source_label"] = source_label

STATUS_LABELS = {
    "new": "Yangi",
    "contacted": "Aloqaga chiqildi",
    "trial": "Sinov darsi",
    "enrolled": "O'qishga yozildi",
    "rejected": "Rad etildi",
}


# ---------------------------------------------------------------------------
# Session auth: login page + the guard used by every admin route
# ---------------------------------------------------------------------------

async def check_auth(request: Request) -> str:
    """Require a valid session cookie for every admin page and admin API.

    GET requests without a session are redirected to the login page;
    POST (API) requests get a 401 so page scripts can show a toast.
    Returns the logged-in login (used as the note author).
    """
    login = verify_session(request.cookies.get(SESSION_COOKIE, ""))
    if login:
        return login
    if request.method == "GET":
        raise HTTPException(status_code=302, headers={"Location": "/admin/login"})
    raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/login")
def login_page(request: Request, error: str = ""):
    """Login form (no session required)."""
    if verify_session(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse(url="/admin/kanban", status_code=302)
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "active": ""}
    )


@router.post("/login")
def do_login(request: Request, login: str = Form(...), password: str = Form(...)):
    """Validate ADMIN_USER / ADMIN_PASSWORD and set the session cookie."""
    if not check_credentials(login.strip(), password):
        log.info("Failed admin login attempt for login=%r", login.strip()[:40])
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Login yoki parol xato.", "active": ""},
            status_code=401,
        )
    response = RedirectResponse(url="/admin/kanban", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        create_session(login.strip()),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    log.info("Admin login: %s", login.strip())
    return response


@router.get("/logout")
def do_logout():
    """Clear the session and return to the login page."""
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


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


def _parse_date(raw: str) -> date | None:
    """YYYY-MM-DD from <input type=date>, or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Sana formati YYYY-MM-DD bo'lishi kerak")


def _enroll_groups() -> list[dict]:
    """Groups for the enrollment dialog (full ones flagged, closed hidden)."""
    from ..models import Student

    result: list[dict] = []
    with SessionLocal() as session:
        groups = session.query(Group).order_by(Group.name, Group.id).all()
        for group in groups:
            if group.status == "tugagan" or not group.is_enabled:
                continue
            capacity = group.capacity or 0
            active = (
                session.query(Student)
                .filter(Student.group_id == group.id, Student.status == "faol")
                .count()
            )
            result.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "full": bool(capacity > 0 and active >= capacity),
                    "capacity": capacity or 0,
                    "active": active,
                }
            )
    return result


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("")
@router.get("/")
def index(_: None = Depends(check_auth)) -> RedirectResponse:
    return RedirectResponse(url="/admin/kanban", status_code=302)


@router.get("/leads")
def leads_page(
    request: Request,
    course: str = "",
    status: str = "",
    q: str = "",
    saved: int = 0,
    user: str = Depends(check_auth),
):
    leads_list = _filtered_leads(course, status, q)
    with SessionLocal() as session:
        course_names = [
            c.name for c in session.query(Course).order_by(Course.sort_order).all()
        ]
        # one query for the latest note of every listed lead
        from sqlalchemy import func

        from ..models import LeadNote

        max_ids = session.query(func.max(LeadNote.id)).group_by(LeadNote.lead_id)
        last_notes = {
            n.lead_id: n
            for n in session.query(LeadNote).filter(LeadNote.id.in_(max_ids)).all()
        }
    query_string = urlencode({"course": course, "status": status, "q": q})
    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            "leads": leads_list,
            "last_notes": last_notes,
            "course_names": course_names,
            "status_labels": STATUS_LABELS,
            "reject_reasons": REJECT_REASONS,
            "sources": SOURCES,
            "employees": get_employees(enabled_only=True),
            "enroll_groups": _enroll_groups(),
            "today": datetime.now(TZ).date(),
            "f_course": course,
            "f_status": status,
            "f_q": q,
            "query_string": query_string,
            "saved": saved,
            "active": "leads",
        },
    )


@router.post("/leads/add")
def add_lead_manual(
    full_name: str = Form(...),
    phone: str = Form(...),
    course: str = Form(""),
    source: str = Form("boshqa"),
    assigned_to: int = Form(0),
    follow_up_at: str = Form(""),
    _: str = Depends(check_auth),
):
    """Lead added by hand from the admin panel (dedup by phone)."""
    full_name = full_name.strip()
    normalized = normalize_phone(phone or "")
    if not full_name or normalized is None:
        raise HTTPException(status_code=400, detail="Ism va to'g'ri telefon kerak")
    if source not in SOURCES or source == "telegram_bot":
        source = "boshqa"  # "Telegram bot" is reserved for bot-generated leads
    lead = upsert_manual_lead(
        full_name=full_name,
        phone=normalized,
        course=course.strip() or None,
        source=source,
        assigned_to=assigned_to or None,
    )
    if follow_up_at:
        with SessionLocal() as session:
            db_lead = session.get(Lead, lead.id)
            if db_lead:
                db_lead.follow_up_at = _parse_date(follow_up_at)
                session.commit()
    log.info("Manual lead saved: id=%s phone=%s source=%s", lead.id, normalized, source)
    return RedirectResponse(url="/admin/leads?saved=1", status_code=303)


def _apply_status(lead_id: int, status: str, reason: str, group_id: int) -> dict:
    """Shared status change: reject reason check + student creation on enroll."""
    if status not in STATUS_LABELS:
        return {"ok": False, "error": "Noma'lum holat", "code": 400}
    if status == "rejected" and reason not in REJECT_REASONS:
        return {"ok": False, "error": "Rad etish sababi tanlanmagan", "code": 400}
    with SessionLocal() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return {"ok": False, "error": "Lid topilmadi", "code": 404}
        lead.status = status
        lead.reject_reason = reason if status == "rejected" else None
        session.commit()

    waiting = False
    student_id = None
    if status == "enrolled":
        student, waiting = enroll_lead(lead_id, group_id or None)
        student_id = student.id
    log.info("Lead %s status -> %s (reason=%s, group=%s)", lead_id, status, reason or "-", group_id or "-")
    return {"ok": True, "status": status, "waiting": waiting, "student_id": student_id}


@router.post("/leads/{lead_id}/status")
def update_lead_status(
    lead_id: int,
    status: str = Form(...),
    reason: str = Form(""),
    group_id: int = Form(0),
    _: str = Depends(check_auth),
):
    """Change a lead's status.

    "Rad etildi" requires a reject reason; "O'qishga yozildi" creates the
    student profile (waiting list if the group is full).
    """
    result = _apply_status(lead_id, status, reason, group_id)
    if not result["ok"]:
        return JSONResponse(result, status_code=result["code"])
    return JSONResponse(result)


@router.post("/leads/{lead_id}/assign")
def assign_lead(
    lead_id: int,
    assigned_to: int = Form(0),
    _: str = Depends(check_auth),
):
    """Attach / detach the employee responsible for a lead."""
    with SessionLocal() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return JSONResponse({"ok": False, "error": "Lid topilmadi"}, status_code=404)
        lead.assigned_to = assigned_to or None
        session.commit()
        log.info("Lead %s assigned_to -> %s", lead_id, assigned_to or "-")
    return JSONResponse({"ok": True, "assigned_to": assigned_to})


@router.post("/leads/{lead_id}/followup")
def set_followup(
    lead_id: int,
    follow_up_at: str = Form(""),
    _: str = Depends(check_auth),
):
    """Set (or clear) the date of the next planned call."""
    with SessionLocal() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return JSONResponse({"ok": False, "error": "Lid topilmadi"}, status_code=404)
        lead.follow_up_at = _parse_date(follow_up_at)
        session.commit()
        log.info("Lead %s follow_up_at -> %s", lead_id, lead.follow_up_at or "-")
    return JSONResponse({"ok": True})


@router.post("/leads/{lead_id}/trial")
def set_trial(
    lead_id: int,
    trial_date: str = Form(""),
    _: str = Depends(check_auth),
):
    """Set (or clear) the trial lesson date."""
    with SessionLocal() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return JSONResponse({"ok": False, "error": "Lid topilmadi"}, status_code=404)
        lead.trial_date = _parse_date(trial_date)
        session.commit()
        log.info("Lead %s trial_date -> %s", lead_id, lead.trial_date or "-")
    return JSONResponse({"ok": True})


@router.get("/leads/{lead_id}/notes")
def notes_list(lead_id: int, _: str = Depends(check_auth)):
    """Full comment history of a lead (JSON for the history modal)."""
    notes = get_lead_notes(lead_id)
    return JSONResponse(
        {
            "ok": True,
            "notes": [
                {
                    "text": n.text,
                    "author": n.author or "—",
                    "created": _format_tashkent(n.created_at),
                }
                for n in notes
            ],
        }
    )


@router.post("/leads/{lead_id}/notes")
def add_note(
    lead_id: int,
    text: str = Form(...),
    user: str = Depends(check_auth),
):
    """Append one comment to a lead's history (author = logged-in user)."""
    text = text.strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Izoh bo'sh"}, status_code=400)
    with SessionLocal() as session:
        if session.get(Lead, lead_id) is None:
            return JSONResponse({"ok": False, "error": "Lid topilmadi"}, status_code=404)
    note = add_lead_note(lead_id, text, author=user)
    return JSONResponse(
        {
            "ok": True,
            "note": {
                "text": note.text,
                "author": note.author or "—",
                "created": _format_tashkent(note.created_at),
            },
        }
    )


@router.get("/leads/export")
def export_csv(course: str = "", status: str = "", q: str = "", _: str = Depends(check_auth)):
    """Download filtered leads as CSV (opens in Excel)."""
    leads_list = _filtered_leads(course, status, q)
    employees = {e.id: e.full_name for e in get_employees()}
    buffer = io.StringIO()
    buffer.write("\ufeff")  # BOM so Excel shows Uzbek letters correctly
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "ID", "Ism", "Telefon", "Kurs", "Username", "Manba", "Mas'ul", "Holat",
        "Rad etish sababi", "Keyingi qo'ng'iroq", "Takroriy", "Yaratildi", "Yangilandi",
    ])
    for lead in leads_list:
        writer.writerow([
            lead.id,
            lead.full_name or "",
            lead.phone or "",
            lead.course or "",
            lead.username or "",
            source_label(lead.source),
            employees.get(lead.assigned_to or 0, ""),
            STATUS_LABELS.get(lead.status, lead.status),
            REJECT_REASONS.get(lead.reject_reason or "", "") if lead.status == "rejected" else "",
            lead.follow_up_at.strftime("%d.%m.%Y") if lead.follow_up_at else "",
            "ha" if (lead.repeat_count or 0) > 0 else "",
            _format_tashkent(lead.created_at),
            _format_tashkent(lead.updated_at),
        ])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


# ---------------------------------------------------------------------------
# Employees (staff; roles prepare per-role visibility for a later stage)
# ---------------------------------------------------------------------------

@router.get("/employees")
def employees_page(request: Request, saved: int = 0, _: str = Depends(check_auth)):
    return templates.TemplateResponse(
        request,
        "employees.html",
        {"employees": get_employees(), "roles": ROLES, "saved": saved, "active": "employees"},
    )


@router.post("/employees/add")
def add_employee(
    full_name: str = Form(...),
    login: str = Form(...),
    role: str = Form("operator"),
    _: str = Depends(check_auth),
):
    full_name = full_name.strip()
    login = login.strip().lower()
    if not full_name or not login:
        raise HTTPException(status_code=400, detail="Ism va login kerak")
    if role not in ROLES:
        raise HTTPException(status_code=400, detail="Noma'lum rol")
    with SessionLocal() as session:
        from ..models import Employee

        exists = session.query(Employee).filter(Employee.login == login).first()
        if not exists:
            session.add(Employee(full_name=full_name, login=login, role=role))
            session.commit()
            log.info("Employee added: %s (%s, %s)", full_name, login, role)
    return RedirectResponse(url="/admin/employees?saved=1", status_code=303)


@router.post("/employees/{employee_id}/update")
def update_employee(
    employee_id: int,
    full_name: str = Form(...),
    login: str = Form(...),
    role: str = Form("operator"),
    is_enabled: str = Form(""),
    _: str = Depends(check_auth),
):
    with SessionLocal() as session:
        from ..models import Employee

        employee = session.get(Employee, employee_id)
        if employee:
            employee.full_name = full_name.strip() or employee.full_name
            employee.login = login.strip().lower() or employee.login
            if role in ROLES:
                employee.role = role
            employee.is_enabled = is_enabled == "on"
            session.commit()
            log.info("Employee updated: id=%s login=%s role=%s", employee_id, employee.login, employee.role)
    return RedirectResponse(url="/admin/employees?saved=1", status_code=303)


@router.post("/employees/{employee_id}/delete")
def delete_employee(employee_id: int, _: str = Depends(check_auth)):
    with SessionLocal() as session:
        from ..models import Employee

        employee = session.get(Employee, employee_id)
        if employee:
            session.delete(employee)
            session.commit()
            log.info("Employee deleted: id=%s login=%s", employee_id, employee.login)
    return RedirectResponse(url="/admin/employees?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Kanban board (CRM pipeline)
# ---------------------------------------------------------------------------

# Column order on the board; "rejected" is the last, styled differently.
KANBAN_STATUSES = ["new", "contacted", "trial", "enrolled", "rejected"]


@router.get("/kanban")
def kanban_page(request: Request, _: str = Depends(check_auth)):
    """'Bugun' dashboard + CRM board: every lead is a card, columns are stages."""
    overview = today_overview()
    today_start_utc = datetime.now(TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    with SessionLocal() as session:
        leads = (
            session.query(Lead)
            .order_by(Lead.updated_at.desc())
            .limit(500)
            .all()
        )
        today_count = (
            session.query(Lead).filter(Lead.created_at >= today_start_utc).count()
        )
    employees = {e.id: e.full_name for e in get_employees()}
    today = datetime.now(TZ).date()
    cards = [
        {
            "id": lead.id,
            "full_name": lead.full_name or "Noma'lum",
            "phone": lead.phone or "—",
            "course": lead.course or "—",
            "status": lead.status,
            "username": lead.username,
            "notes": lead.notes,
            "source": source_label(lead.source),
            "assignee": employees.get(lead.assigned_to or 0),
            "follow_up_at": lead.follow_up_at.isoformat() if lead.follow_up_at else "",
            "follow_up_overdue": bool(
                lead.follow_up_at and lead.follow_up_at < today
            ),
            "trial_date": lead.trial_date.isoformat() if lead.trial_date else "",
            "trial_due": bool(lead.trial_date and lead.trial_date <= today),
            "repeat": (lead.repeat_count or 0) > 0,
            "reject_reason": REJECT_REASONS.get(lead.reject_reason or ""),
            "updated": _format_tashkent(lead.updated_at),
        }
        for lead in leads
    ]
    columns = [
        {"status": status, "label": STATUS_LABELS[status]}
        for status in KANBAN_STATUSES
    ]
    enroll_groups = _enroll_groups()
    return templates.TemplateResponse(
        request,
        "kanban.html",
        {
            "columns": columns,
            "cards": cards,
            "reject_reasons": REJECT_REASONS,
            "enroll_groups": enroll_groups,
            "overview": overview,
            "today_count": today_count,
            "total_count": len(cards),
            "active": "kanban",
        },
    )


@router.post("/leads/{lead_id}/move")
def move_lead(
    lead_id: int,
    status: str = Form(...),
    reason: str = Form(""),
    group_id: int = Form(0),
    _: str = Depends(check_auth),
):
    """JSON endpoint for the kanban drag & drop (card moved to a new column)."""
    result = _apply_status(lead_id, status, reason, group_id)
    if not result["ok"]:
        return JSONResponse(result, status_code=result["code"])
    return JSONResponse({"ok": True, "lead_id": lead_id, "status": status,
                         "waiting": result.get("waiting", False)})


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
        {
            "courses": courses,
            "teachers": get_teachers(),
            "saved": saved,
            "active": "courses",
        },
    )


@router.post("/courses/add")
def add_course(
    name: str = Form(...),
    sort_order: int = Form(0),
    price: str = Form(""),
    price_sum: int = Form(0),
    duration: str = Form(""),
    description: str = Form(""),
    teacher_id: int = Form(0),
    _: None = Depends(check_auth),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Course name is required")
    with SessionLocal() as session:
        exists = session.query(Course).filter(Course.name == name).first()
        if not exists:
            session.add(
                Course(
                    name=name,
                    sort_order=sort_order,
                    is_enabled=True,
                    price=price.strip() or None,
                    price_sum=price_sum or None,
                    duration=duration.strip() or None,
                    description=description.strip() or None,
                    teacher_id=teacher_id or None,
                )
            )
            session.commit()
            log.info("Course added: %s (price_sum=%s)", name, price_sum or "-")
    return RedirectResponse(url="/admin/courses?saved=1", status_code=303)


@router.post("/courses/{course_id}/update")
def update_course(
    course_id: int,
    name: str = Form(...),
    sort_order: int = Form(0),
    is_enabled: str = Form(""),
    price: str = Form(""),
    price_sum: int = Form(0),
    duration: str = Form(""),
    description: str = Form(""),
    teacher_id: int = Form(0),
    _: None = Depends(check_auth),
):
    with SessionLocal() as session:
        course = session.get(Course, course_id)
        if course:
            course.name = name.strip() or course.name
            course.sort_order = sort_order
            course.is_enabled = is_enabled == "on"  # checkbox: present = "on"
            course.price = price.strip() or None
            course.price_sum = price_sum or None
            course.duration = duration.strip() or None
            course.description = description.strip() or None
            course.teacher_id = teacher_id or None
            session.commit()
            log.info("Course updated: id=%s name=%s enabled=%s price_sum=%s",
                     course_id, course.name, course.is_enabled, course.price_sum or "-")
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
# Teachers (CRM -> Claude knowledge)
# ---------------------------------------------------------------------------

@router.get("/teachers")
def teachers_page(request: Request, saved: int = 0, _: None = Depends(check_auth)):
    return templates.TemplateResponse(
        request,
        "teachers.html",
        {"teachers": get_teachers(), "saved": saved, "active": "teachers"},
    )


@router.post("/teachers/add")
def add_teacher(
    full_name: str = Form(...),
    phone: str = Form(""),
    bio: str = Form(""),
    _: None = Depends(check_auth),
):
    full_name = full_name.strip()
    if not full_name:
        raise HTTPException(status_code=400, detail="Teacher name is required")
    with SessionLocal() as session:
        session.add(
            Teacher(
                full_name=full_name,
                phone=phone.strip() or None,
                bio=bio.strip() or None,
                is_enabled=True,
            )
        )
        session.commit()
        log.info("Teacher added: %s", full_name)
    return RedirectResponse(url="/admin/teachers?saved=1", status_code=303)


@router.post("/teachers/{teacher_id}/update")
def update_teacher(
    teacher_id: int,
    full_name: str = Form(...),
    phone: str = Form(""),
    bio: str = Form(""),
    is_enabled: str = Form(""),
    _: None = Depends(check_auth),
):
    with SessionLocal() as session:
        teacher = session.get(Teacher, teacher_id)
        if teacher:
            teacher.full_name = full_name.strip() or teacher.full_name
            teacher.phone = phone.strip() or None
            teacher.bio = bio.strip() or None
            teacher.is_enabled = is_enabled == "on"
            session.commit()
            log.info("Teacher updated: id=%s name=%s", teacher_id, teacher.full_name)
    return RedirectResponse(url="/admin/teachers?saved=1", status_code=303)


@router.post("/teachers/{teacher_id}/delete")
def delete_teacher(teacher_id: int, _: None = Depends(check_auth)):
    with SessionLocal() as session:
        teacher = session.get(Teacher, teacher_id)
        if teacher:
            session.delete(teacher)
            session.commit()
            log.info("Teacher deleted: id=%s name=%s", teacher_id, teacher.full_name)
    return RedirectResponse(url="/admin/teachers?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Groups (CRM -> Claude knowledge)
# ---------------------------------------------------------------------------

@router.get("/groups")
def groups_page(request: Request, saved: int = 0, _: None = Depends(check_auth)):
    from ..models import Student

    with SessionLocal() as session:
        groups = session.query(Group).order_by(Group.id).all()
        active_counts = dict(
            session.query(Student.group_id, func.count(Student.id))
            .filter(Student.status == "faol", Student.group_id.isnot(None))
            .group_by(Student.group_id)
            .all()
        )
    return templates.TemplateResponse(
        request,
        "groups.html",
        {
            "groups": groups,
            "courses": get_all_courses(),
            "teachers": get_teachers(),
            "group_statuses": GROUP_STATUSES,
            "g_active": active_counts,
            "saved": saved,
            "active": "groups",
        },
    )


@router.post("/groups/add")
def add_group(
    name: str = Form(...),
    course_id: int = Form(0),
    teacher_id: int = Form(0),
    schedule: str = Form(""),
    start_date: str = Form(""),
    starts_on: str = Form(""),
    capacity: int = Form(0),
    _: None = Depends(check_auth),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    with SessionLocal() as session:
        session.add(
            Group(
                name=name,
                course_id=course_id or None,
                teacher_id=teacher_id or None,
                schedule=schedule.strip() or None,
                start_date=start_date.strip() or None,
                starts_on=_parse_date(starts_on),
                capacity=capacity or None,
                status="ochiq",
                is_enabled=True,
            )
        )
        session.commit()
        log.info("Group added: %s (capacity=%s)", name, capacity or "-")
    return RedirectResponse(url="/admin/groups?saved=1", status_code=303)


@router.post("/groups/{group_id}/update")
def update_group(
    group_id: int,
    name: str = Form(...),
    course_id: int = Form(0),
    teacher_id: int = Form(0),
    schedule: str = Form(""),
    start_date: str = Form(""),
    starts_on: str = Form(""),
    capacity: int = Form(0),
    status: str = Form(""),
    is_enabled: str = Form(""),
    _: None = Depends(check_auth),
):
    from ..services import refresh_group_status

    with SessionLocal() as session:
        group = session.get(Group, group_id)
        if group:
            group.name = name.strip() or group.name
            group.course_id = course_id or None
            group.teacher_id = teacher_id or None
            group.schedule = schedule.strip() or None
            group.start_date = start_date.strip() or None
            group.starts_on = _parse_date(starts_on)
            group.capacity = capacity or None
            # Manual close wins; otherwise the auto fill state is recomputed.
            group.status = status if status in GROUP_STATUSES else None
            group.is_enabled = is_enabled == "on"
            session.commit()
            if group.status != "tugagan":
                refresh_group_status(group_id)
            log.info("Group updated: id=%s name=%s capacity=%s status=%s",
                     group_id, group.name, capacity or "-", group.status or "-")
    return RedirectResponse(url="/admin/groups?saved=1", status_code=303)


@router.post("/groups/{group_id}/delete")
def delete_group(group_id: int, _: None = Depends(check_auth)):
    with SessionLocal() as session:
        group = session.get(Group, group_id)
        if group:
            session.delete(group)
            session.commit()
            log.info("Group deleted: id=%s name=%s", group_id, group.name)
    return RedirectResponse(url="/admin/groups?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Debtors
# ---------------------------------------------------------------------------

@router.get("/debtors")
def debtors_page(request: Request, sort: str = "debt", _: str = Depends(check_auth)):
    """Students with a debt; sortable by debt amount or days late."""
    rows = get_debtors()
    if sort == "late":
        rows.sort(key=lambda r: r["days_late"], reverse=True)
    return templates.TemplateResponse(
        request,
        "debtors.html",
        {"rows": rows, "sort": sort, "active": "debtors"},
    )


# ---------------------------------------------------------------------------
# Students (created automatically when a lead is enrolled)
# ---------------------------------------------------------------------------

@router.get("/students")
def students_page(
    request: Request,
    q: str = "",
    group_id: int = 0,
    status: str = "",
    student_id: int = 0,
    _: str = Depends(check_auth),
):
    if student_id:
        # direct link from the debtors page — show only this student's card
        students_list = get_students_filtered("", 0, "")
        students_list = [s for s in students_list if s.id == student_id]
    else:
        students_list = get_students_filtered(q, group_id, status)
    with SessionLocal() as session:
        groups = session.query(Group).order_by(Group.name, Group.id).all()
    query_string = urlencode({"q": q, "group_id": group_id, "status": status})
    return templates.TemplateResponse(
        request,
        "students.html",
        {
            "students": students_list,
            "groups": groups,
            "student_statuses": STUDENT_STATUSES,
            "payment_methods": PAYMENT_METHODS,
            "f_q": q,
            "f_group": group_id,
            "f_status": status,
            "query_string": query_string,
            "active": "students",
        },
    )


@router.post("/students/{student_id}/update")
def update_student(
    student_id: int,
    full_name: str = Form(""),
    phone: str = Form(""),
    group_id: int = Form(0),
    status: str = Form(""),
    discount_type: str = Form(""),
    discount_value: int = Form(0),
    _: str = Depends(check_auth),
):
    """Auto-save a student row (name, phone, group, status, discount)."""
    from ..models import utcnow
    from ..services import refresh_group_status

    with SessionLocal() as session:
        student = session.get(Student, student_id)
        if student is None:
            return JSONResponse({"ok": False, "error": "Talaba topilmadi"}, status_code=404)
        if full_name.strip():
            student.full_name = full_name.strip()
        if phone.strip():
            normalized = normalize_phone(phone)
            if normalized is None:
                return JSONResponse({"ok": False, "error": "Telefon noto'g'ri"}, status_code=400)
            student.phone = normalized
        old_group = student.group_id
        student.group_id = group_id or None
        if status in STUDENT_STATUSES:
            student.status = status
        # discount: "sum" (so'm) or "percent"; empty clears it
        if discount_type in ("sum", "percent"):
            if discount_value < 0 or (discount_type == "percent" and discount_value > 100):
                return JSONResponse(
                    {"ok": False, "error": "Chegirma qiymati noto'g'ri"}, status_code=400
                )
            student.discount_type = discount_type
            student.discount_value = discount_value or None
        elif discount_type == "none":
            student.discount_type = None
            student.discount_value = None
        student.updated_at = utcnow()
        session.commit()
    for gid in {old_group, group_id or None} - {None}:
        refresh_group_status(gid)
    log.info("Student %s updated (status=%s group=%s discount=%s)",
             student_id, status or "-", group_id or "-", discount_type or "-")
    return JSONResponse({"ok": True})


@router.get("/students/{student_id}/card")
def student_card(student_id: int, _: str = Depends(check_auth)):
    """Student card JSON: info, group, money summary, periods, payments."""
    with SessionLocal() as session:
        student = session.get(Student, student_id)
        if student is None:
            return JSONResponse({"ok": False, "error": "Talaba topilmadi"}, status_code=404)
        course = None
        group = None
        if student.group_id:
            group = session.get(Group, student.group_id)
            course = session.get(Course, group.course_id) if group else None
        payments = (
            session.query(Payment)
            .filter(Payment.student_id == student_id)
            .order_by(Payment.id.desc())
            .all()
        )
        notes = (
            session.query(StudentNote)
            .filter(StudentNote.student_id == student_id)
            .order_by(StudentNote.id.desc())
            .limit(20)
            .all()
        )

    price = monthly_price(student, course)
    debt = student_debt(student, course)
    periods = student_periods(student, course)
    return JSONResponse(
        {
            "ok": True,
            "student": {
                "id": student.id,
                "full_name": student.full_name or "—",
                "phone": student.phone or "—",
                "username": student.username or "",
                "status": student.status,
                "group": group.name if group else None,
                "course": course.name if course else None,
                "source": source_label(student.source),
                "enrolled": student.enrolled_at.strftime("%d.%m.%Y") if student.enrolled_at else "—",
                "discount": (
                    f"-{student.discount_value}%" if student.discount_type == "percent"
                    else f"-{_format_sum(student.discount_value)} so'm" if student.discount_type == "sum"
                    else ""
                ),
            },
            "money": {
                "monthly_price": price,
                "total_expected": debt["total_expected"],
                "paid_total": debt["paid_total"],
                "debt": debt["debt"],
                "days_late": debt["days_late"],
                "price_set": bool(price > 0),
            },
            "periods": periods,
            "payments": [
                {
                    "id": p.id,
                    "amount": p.amount,
                    "date": p.paid_date.strftime("%d.%m.%Y") if p.paid_date else "",
                    "method": PAYMENT_METHODS.get(p.method or "", p.method or "—"),
                    "period": p.period or "—",
                    "note": p.note or "",
                    "created_by": p.created_by or "—",
                }
                for p in payments
            ],
            "notes": [
                {"text": n.text, "author": n.author or "—", "created": _format_tashkent(n.created_at)}
                for n in notes
            ],
        }
    )


@router.post("/payments/add")
def add_payment_route(
    student_id: int = Form(...),
    amount: int = Form(...),
    paid_date: str = Form(""),
    method: str = Form("naqd"),
    period: str = Form(""),
    note: str = Form(""),
    user: str = Depends(check_auth),
):
    """Add a payment (whole so'm). period = YYYY-MM the payment covers."""
    if amount <= 0:
        return JSONResponse({"ok": False, "error": "Summa musbat bo'lishi kerak"}, status_code=400)
    with SessionLocal() as session:
        if session.get(Student, student_id) is None:
            return JSONResponse({"ok": False, "error": "Talaba topilmadi"}, status_code=404)
    period = (period or "").strip()
    if period and (len(period) != 7 or not period[:4].isdigit() or period[4] != "-" or not period[5:].isdigit()):
        return JSONResponse({"ok": False, "error": "Davr formati YYYY-MM"}, status_code=400)
    payment = add_payment(
        student_id=student_id,
        amount=amount,
        paid_date=_parse_date(paid_date) or datetime.now(TZ).date(),
        method=method,
        period=period or None,
        note=note,
        created_by=user,
    )
    return JSONResponse(
        {"ok": True, "payment": {"id": payment.id, "amount": payment.amount}}
    )


@router.post("/payments/{payment_id}/delete")
def delete_payment_route(payment_id: int, _: str = Depends(check_auth)):
    with SessionLocal() as session:
        payment = session.get(Payment, payment_id)
        if payment:
            session.delete(payment)
            session.commit()
            log.info("Payment deleted: id=%s", payment_id)
    return JSONResponse({"ok": True})


@router.post("/students/{student_id}/delete")
def delete_student(student_id: int, _: str = Depends(check_auth)):
    from ..services import refresh_group_status

    with SessionLocal() as session:
        student = session.get(Student, student_id)
        if student:
            group_id = student.group_id
            session.delete(student)
            session.commit()
            if group_id:
                refresh_group_status(group_id)
            log.info("Student deleted: id=%s", student_id)
    return JSONResponse({"ok": True})


@router.get("/students/{student_id}/notes")
def student_notes_list(student_id: int, _: str = Depends(check_auth)):
    notes = get_student_notes(student_id)
    return JSONResponse(
        {
            "ok": True,
            "notes": [
                {
                    "text": n.text,
                    "author": n.author or "—",
                    "created": _format_tashkent(n.created_at),
                }
                for n in notes
            ],
        }
    )


@router.post("/students/{student_id}/notes")
def add_student_note_route(
    student_id: int,
    text: str = Form(...),
    user: str = Depends(check_auth),
):
    text = text.strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Izoh bo'sh"}, status_code=400)
    with SessionLocal() as session:
        if session.get(Student, student_id) is None:
            return JSONResponse({"ok": False, "error": "Talaba topilmadi"}, status_code=404)
    note = add_student_note(student_id, text, author=user)
    return JSONResponse(
        {
            "ok": True,
            "note": {
                "text": note.text,
                "author": note.author or "—",
                "created": _format_tashkent(note.created_at),
            },
        }
    )


# ---------------------------------------------------------------------------
# AI answers (Claude): style prompt, extra knowledge, sandbox test window
# ---------------------------------------------------------------------------

AI_TEXT_LIMIT = 8000  # safety cap for the textareas


def _ai_page_context(
    saved: int = 0,
    test_message: str = "",
    test_reply: str | None = None,
    test_lead: str | None = None,
    test_error: str | None = None,
) -> dict:
    """Template context for the AI Javoblar page (GET and test POST)."""
    return {
        "style_prompt": get_ai_text(AI_STYLE_KEY) or claude_client.DEFAULT_STYLE_PROMPT,
        "extra_knowledge": get_ai_text(AI_KNOWLEDGE_KEY) or "",
        "knowledge_files": [name for name, _ in claude_client.load_knowledge()],
        "claude_enabled": claude_client.is_enabled(),
        "model": settings.CLAUDE_MODEL,
        "test_message": test_message,
        "test_reply": test_reply,
        "test_lead": test_lead,
        "test_error": test_error,
        "saved": saved,
        "active": "ai",
    }


@router.get("/ai")
def ai_page(request: Request, saved: int = 0, _: None = Depends(check_auth)):
    return templates.TemplateResponse(
        request, "ai.html", _ai_page_context(saved=saved)
    )


@router.post("/ai")
def save_ai(
    style_prompt: str = Form(...),
    extra_knowledge: str = Form(""),
    _: None = Depends(check_auth),
):
    # Empty style -> the code default (professional preset) is used again.
    set_ai_text(AI_STYLE_KEY, style_prompt.strip()[:AI_TEXT_LIMIT])
    set_ai_text(AI_KNOWLEDGE_KEY, extra_knowledge.strip()[:AI_TEXT_LIMIT])
    log.info("AI settings saved: style=%d chars, extra knowledge=%d chars",
             len(style_prompt), len(extra_knowledge))
    return RedirectResponse(url="/admin/ai?saved=1", status_code=303)


@router.post("/ai/test")
def test_ai(request: Request, message: str = Form(...), _: None = Depends(check_auth)):
    """Sandbox: run one visitor message through Claude with the SAVED settings.

    Nothing is sent to any chat — the reply is only shown on this page.
    """
    message = message.strip()
    test_reply: str | None = None
    test_lead: str | None = None
    test_error: str | None = None

    if not claude_client.is_enabled():
        test_error = (
            "ANTHROPIC_API_KEY sozlanmagan — sinov mumkin emas. Kalit "
            "(Railway → Variables) qo'shilgach, bu oyna ishlaydi."
        )
    elif message:
        reply = claude_client.generate_reply([{"role": "user", "text": message}])
        if reply is None:
            test_error = (
                "Claude javob bermadi (API xatosi — Railway loglaridagi "
                "'Claude API' yozuvlarini tekshiring)."
            )
        else:
            visible, name, phone, course = claude_client.extract_lead(reply)
            test_reply = visible or "(javob faqat lid belgisidan iborat bo'ldi)"
            if phone:
                test_lead = f"{name or '—'} | {phone} | {course or '—'}"
    else:
        test_error = "Xabar matni bo'sh."

    return templates.TemplateResponse(
        request,
        "ai.html",
        _ai_page_context(
            test_message=message,
            test_reply=test_reply,
            test_lead=test_lead,
            test_error=test_error,
        ),
    )

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
            "reminders_enabled": get_setting("reminders_enabled", "1") == "1",
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
    reminders_enabled: str = Form(""),
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
    set_setting("reminders_enabled", "1" if reminders_enabled == "on" else "0")
    log.info("Settings saved: work %s-%s, owner=%s, reminders=%s",
             work_start, work_end, owner or "-", reminders_enabled == "on")
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)


@router.post("/reminders/test")
def reminders_test(_: None = Depends(check_auth)):
    """'Test xabar yuborish' — one message to the owner chat id."""
    ok, message = reminders.send_test_message()
    log.info("Reminder test: ok=%s", ok)
    return JSONResponse({"ok": ok, "message": message})
