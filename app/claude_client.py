"""Claude (Anthropic Messages API) client for answering business messages.

The system prompt is built from every .md / .txt file inside knowledge/,
so the owner teaches the bot by simply editing those files — no redeploy
needed (files are re-read on every message).

If ANTHROPIC_API_KEY is missing, is_enabled() returns False and the handlers
fall back to the built-in button flow.
"""
import logging
import re
from pathlib import Path

import httpx

from .config import settings
from .services import AI_KNOWLEDGE_KEY, AI_STYLE_KEY, get_ai_text

log = logging.getLogger("bot.claude")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Folder with knowledge files, at the project root (next to app/).
KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
KNOWLEDGE_EXTENSIONS = (".md", ".txt")

# Claude can be slow on long knowledge bases / busy hours.
_client = httpx.Client(timeout=60)

# (signature, docs) cache: files are re-read only when name/size/mtime changes.
_knowledge_cache: tuple[tuple, list[tuple[str, str]]] = ((), [])

# Marker Claude appends when it has collected a lead:
#   [LEAD] Ism Familiya | +998XXXXXXXXX | Kurs nomi [/LEAD]
LEAD_RE = re.compile(
    r"\[LEAD\]\s*(.*?)\s*\|\s*(.*?)\s*(?:\|\s*(.*?)\s*)?\[/LEAD\]", re.DOTALL
)


def is_enabled() -> bool:
    """True when Claude answers are possible (API key configured)."""
    return bool(settings.ANTHROPIC_API_KEY)


# Professional answer style used until the owner saves their own version in
# the admin panel (AI Javoblar page). Empty DB value -> this default.
DEFAULT_STYLE_PROMPT = (
    "Uslub — professional ta'lim markazi konsultanti:\n"
    "- Mijozga hurmat bilan, doim \"siz\" deb murojaat qilasan; toning — ishonchli, "
    "iliq va rasmiy.\n"
    "- Javob tuzilgan holda: avval savolga to'g'ridan-to'g'ri aniq javob, so'ng 1-2 "
    "qatorda muhim tafsilot (davomiylik, narx, jadval — faqat bilimlarda bor bo'lsa).\n"
    "- Qisqa yozasan: odatda 3-5 qator; ortiqcha so'z, takror va ortiqcha emoji "
    "ishlatmaysan (eng ko'pi 1 ta emoji yoki umuman yo'q).\n"
    "- Adabiy, ravon o'zbek tilida yozasan: tinish belgilari to'g'ri, ortiqcha chet "
    "so'zlarisiz.\n"
    "- Bilmaganingda taxmin qilmaysan: \"bu bo'yicha mutaxassislarimiz aniq ma'lumot "
    "beradi\" deysan va aloqada qolishni taklif qilasan.\n"
    "- Mijoz qiziqsa, suhbatni professional yakunlaysan: ism va telefon so'raysan, "
    "\"o'qishga yozilish bo'yicha mutaxassislarimiz siz bilan bog'lanadi\" deysan.\n"
    "- Bosim o'tkazmaysan va bo'sh va'da bermaysan (\"100% kafolat\", \"eng arzon\" "
    "kabi iboralar taqiqlangan)."
)


# ---------------------------------------------------------------------------
# knowledge/ files
# ---------------------------------------------------------------------------

def load_knowledge() -> list[tuple[str, str]]:
    """[(filename, content), ...] from knowledge/, re-read only on change."""
    global _knowledge_cache
    files: list[Path] = []
    if KNOWLEDGE_DIR.is_dir():
        files = sorted(
            path
            for path in KNOWLEDGE_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in KNOWLEDGE_EXTENSIONS
        )
    try:
        signature = tuple(
            (path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in files
        )
    except OSError:
        signature, files = (), []
    if signature == _knowledge_cache[0]:
        return _knowledge_cache[1]

    docs: list[tuple[str, str]] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            log.warning("Cannot read knowledge file: %s", path)
            continue
        docs.append((path.name, content))
    _knowledge_cache = (signature, docs)
    log.info("Knowledge loaded: %s file(s) from %s", len(docs), KNOWLEDGE_DIR)
    return docs


def knowledge_file_count() -> int:
    """Number of knowledge files currently in use (for startup logs/tests)."""
    return len(load_knowledge())


def build_system_prompt() -> str:
    """System prompt: role + knowledge (files + admin extra) + answer style.

    The style block comes from the admin panel (AI Javoblar page) and decides
    how professional the answers sound. The [LEAD] rules are fixed in code so
    lead collection can never break by editing the panel.
    """
    docs = load_knowledge()
    extra = (get_ai_text(AI_KNOWLEDGE_KEY) or "").strip()
    knowledge_blocks = [f"### {name}\n{content}" for name, content in docs if content]
    if extra:
        knowledge_blocks.append(f"### Qo'shimcha ma'lumot (admin paneldan)\n{extra}")
    if knowledge_blocks:
        knowledge = "\n\n".join(knowledge_blocks)
        knowledge_note = (
            "Quyidagi bilimlarga asoslanib javob ber "
            "(bu — markazning rasmiy ma'lumotlari):"
        )
    else:
        knowledge = "(bilim fayllari topilmadi)"
        knowledge_note = (
            "Bilim fayllari bo'sh — aniq raqam/narx aytmaysan, umumiy javob ber"
        )

    style = (get_ai_text(AI_STYLE_KEY) or "").strip() or DEFAULT_STYLE_PROMPT
    return (
        "Sen \"Inventor Innovation Center\" (O'zbekistondagi ta'lim markazi) "
        "Telegram Business chatining yordamchisisan. Mijozlar bilan o'zbek "
        "tilida (lotin alifbosida) muloqot qilasan.\n\n"
        f"{knowledge_note}\n\n{knowledge}\n\n"
        f"Javob uslubi (muhim — shu qoidalarga qat'iy amal qil):\n{style}\n\n"
        "Lid yig'ish qoidalari (o'zgarmas):\n"
        "1. Mijoz kursga yozilmoqchi bo'lsa yoki narx/jadval so'rasa, uning ISMI "
        "va TELEFON raqamini so'ra (avval ism, keyin telefon). Telefonni "
        "+998XXXXXXXXX, 998XXXXXXXXX yoki 9XXXXXXXX ko'rinishida qabul qil.\n"
        "2. Ism VA telefon to'liq bo'lgach, javobing oxiriga aynan shu ko'rinishda "
        "belgi qo'sh (bu belgini bot o'qiydi, mijoz ko'rmaydi — uni hech qachon "
        "boshqa maqsadda ishlatma):\n"
        "[LEAD] Ism Familiya | +998XXXXXXXXX | Kurs nomi [/LEAD]\n"
        "3. Kurs nomini bilimlardagi ro'yxatdan aynan tanlashga harakat qil.\n"
        "4. O'zingni AI model (Claude va h.k.) deb tanishtirma — sen markazning "
        "yordamchisisan."
    )


# ---------------------------------------------------------------------------
# Anthropic API call
# ---------------------------------------------------------------------------

def _merge_messages(history: list[dict]) -> list[dict]:
    """Anthropic requires alternating roles starting with 'user'.

    Consecutive messages with the same role are joined; leading assistant
    messages are dropped.
    """
    merged: list[dict] = []
    for message in history:
        role = message.get("role")
        text = (message.get("text") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + text
        else:
            merged.append({"role": role, "content": text})
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


def _post_messages(payload: dict) -> dict:
    """One POST to the Anthropic Messages API. Returns the parsed JSON body.

    Kept as a tiny separate function so tests can monkeypatch it.
    """
    response = _client.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": settings.ANTHROPIC_API_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json=payload,
    )
    return response.json()


def generate_reply(history: list[dict]) -> str | None:
    """Generate an answer for the chat history (oldest first, dicts with
    "role" and "text"). Returns None on any problem — the caller then falls
    back to the built-in flow so the visitor is never left without a reply."""
    if not is_enabled():
        return None
    messages = _merge_messages(history)
    if not messages:
        return None
    payload = {
        "model": settings.CLAUDE_MODEL,
        "max_tokens": settings.CLAUDE_MAX_TOKENS,
        "system": build_system_prompt(),
        "messages": messages,
    }
    try:
        data = _post_messages(payload)
    except Exception:
        log.exception("Claude API request failed (model=%s)", settings.CLAUDE_MODEL)
        return None
    if data.get("type") == "error":
        log.error(
            "Claude API error: %s (model=%s)", data.get("error"), settings.CLAUDE_MODEL
        )
        return None
    text = "".join(
        block.get("text", "")
        for block in data.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    return text or None


# ---------------------------------------------------------------------------
# [LEAD] marker parsing
# ---------------------------------------------------------------------------

def extract_lead(reply: str) -> tuple[str, str | None, str | None, str | None]:
    """Split a Claude reply into (visible_text, name, phone, course).

    The [LEAD] ... [/LEAD] block is cut out of the visible text. Missing or
    empty parts come back as None.
    """
    match = LEAD_RE.search(reply)
    if match is None:
        return reply.strip(), None, None, None
    visible = LEAD_RE.sub("", reply).strip()
    name = (match.group(1) or "").strip(" |-") or None
    phone = (match.group(2) or "").strip() or None
    course = (match.group(3) or "").strip(" |-") or None
    return visible, name, phone, course
