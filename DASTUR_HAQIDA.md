# Inventor Innovation Center — Telegram Business Bot

**Dastur haqida va uning imkoniyatlari** (to'liq ko'rsatkich)

> Bu hujjat loyihaning umumiy tavsifi va imkoniyatlari ro'yxatidir.
> O'rnatish bosqichma-bosqich yo'riqnomasi uchun [README.md](README.md) fayliga qarang.

---

## 1. Dastur nima?

Bu — **"Inventor Innovation Center" o'quv markazi** uchun ishlab chiqilgan
Telegram bot + CRM tizimi. Markazning Telegram Premium hisobiga kelgan har bir
xabar (**Telegram Business → Chatbots** rejimida) ushbu bot tomonidan avtomatik
javoblanadi:

- Mijoz bilan suhbat quradi va **lid (mijoz ma'lumotlarini)** yig'adi:
  kurs → ism → telefon raqam.
- Lidlar ma'lumotlar bazasida saqlanadi (Railway PostgreSQL yoki lokal SQLite).
- **Veb-admin panel** orqali barcha jarayonlar boshqariladi:
  lidlar, talabalar, to'lovlar, guruhlar, hisobotlar va h.k.
- Xohlanganda **Claude (Anthropic) sun'iy intellekti** mijoz savollariga
  o'zini javob beradi.

Barcha xabarlar va admin panel **o'zbek tilida (lotin yozuvida)**.

---

## 2. Texnik arxitektura

| Komponent | Fayl | Vazifasi |
|---|---|---|
| FastAPI ilova | `app/main.py` | Webhook (darhol 200 + dublikat nazorati), health check, ishga tushirish |
| Sozlamalar | `app/config.py` | Muhit o'zgaruvchilarini o'qish |
| Ma'lumotlar bazasi | `app/database.py`, `app/models.py` | Jadvallarni avtomatik yaratish; leads, students, payments, courses, groups... |
| Biznes-logika | `app/services.py` | Suhbat oqimi, validatsiya, lidlar, chat tarixi |
| Telegram handler | `app/handlers.py` | Har bir Telegram xabariga javob (butun suhbat) |
| AI (Claude) | `app/claude_client.py` | Business-chatlar uchun AI javoblar |
| Telegram API | `app/telegram_client.py` | httpx orqali Bot API chaqiruvlari |
| CRM adapteri | `app/crm.py` | Tashqi CRM'ga lid uzatish (hozircha shablon) |
| Eslatmalar | `app/followup.py`, `app/reminders.py` | 24-soat follow-up va tizimli eslatmalar |
| Zaxira | `app/backup.py` | Avtomatik va qo'lda ma'lumotlar bazasi zaxirasi |
| Admin panel | `app/admin/routes.py` + `app/templates/` | Barcha veb-sahifalar (o'zbek tilida) |
| Bilim bazasi | `knowledge/*.md` | Claude uchun markaz haqidagi bilimlar (erkin tahrirlanadi) |
| Test | `smoke_test.py` | To'liq suhbatni simulyatsiya qiluvchi avtomatik test |

Jadvallar **birinchi ishga tushishda avtomatik** yaratiladi — migratsiya buyrug'i kerak emas.

---

## 3. Bot imkoniyatlari

### 3.1. Mijoz suhbati (built-in oqim)

```
Mijoz:  (istalgan xabar)
Bot:    Assalomu alaykum ... kurslardan birini tanlang: [tugmalar]
Mijoz:  (kurs tugmasini bosadi yoki 1 / kurs nomini yozadi)
Bot:    Javobingiz uchun rahmat! Ismingizni yozib qoldiring.
Mijoz:  Aziza Karimova
Bot:    Telefon raqamingizni qoldiring, admin tez orada aloqaga chiqadi.
Mijoz:  +998901234567  (qabul qilinadi: 998901234567, 901234567)
Bot:    Rahmat! Ma'lumotlaringiz qabul qilindi, admin tez orada bog'lanadi.
Egasi:  shaxsiy chatda xabar oladi: ism, telefon, kurs, username, vaqt
```

- `/start` yoki **"bekor qilish"** — suhbatni istalgan paytda qayta boshlaydi.
- Telefon validatsiyasi: noto'g'ri raqamda bot muloyim ravishda qayta so'raydi.
- **Dublikatsizlik:** bir xil Telegram foydalanuvchi yoki telefon — yangi lid
  yaratmaydi, mavjud yozuv yangilanadi.
- Tahrirlangan xabarlar: ism kutilayotganda ism tahrirlansa yangilanadi,
  boshqa tahrirlar dublikat yaratmaydi.
- **24-soatlik follow-up:** ochiq lid 24 soat javobsiz qolsa, egasiga (mijozga
  emas) **bir marta** eslatma yuboriladi.
- Ish vaqtidan tashqarida bot qo'shimcha yozadi:
  *"Hozir ish vaqtimiz tugagan, ertaga javob beramiz."*
- Suhbat faqat **shaxsiy chatlarda** ishlaydi; eganing o'z xabarlariga javob
  bermaydi — siz mijozlar bilan o'zingiz yozishda davom etasiz.

### 3.2. Claude AI javoblar (ixtiyoriy, `ANTHROPIC_API_KEY` bilan)

- Business-chatdagi **har bir xabarga Claude** (`CLAUDE_MODEL`) javob beradi —
  tugmali oqim o'rniga tabiiy, kontekstli suhbat.
- **Bilim manbalari:**
  - `knowledge/` papkasidagi barcha `.md` / `.txt` fayllar — har bir xabarda
    qayta o'qiladi, **qayta deploy shart emas**;
  - **Jonli CRM bilimlari:** kurslar (narx, davomiylik, o'qituvchi, tavsif),
    o'qituvchilar (tarjimai hol), guruhlar (jadval, joylar soni, to'lganlik
    holati) har xabarda tizimli promtga qayta quriladi — admin panelga yangi
    kurs qo'shilsa, bot darhol javob bera oladi.
- **Javob uslubi** admin panelda **AI Javoblar** sahifasida sozlanadi:
  professional uslub promti + qo'shimcha bilim maydoni.
  Lid yig'ish qoidalari (`[LEAD]` belgisi) koddagi kabi o'zgarmas.
- **Sandbox (qumdon) test oynasi:** mijoz xabarini yozib, Claude javobini va
  aniqlangan lidni real chatga yubormasdan ko'rish mumkin.
- Claude chatdagi **oxirgi 16 xabarni** ko'radi — javoblar kontekstda bo'ladi.
- Mijozning **ismi + telefoni** yig'ilganda bot ichki `[LEAD]` belgisi orqali
  lidni saqlaydi va egasiga xabar beradi.
- **Zaxira mexanizmi:** kalit yo'q yoki API xato bersa, built-in tugmali oqim
  avtomatik ishga tushadi — mijoz hech qachon javobsiz qolmaydi.

### 3.3. Ega uchun Telegram buyruqlari

Faqat `OWNER_CHAT_ID` egasiga ishlaydi:

| Buyruq | Nima ko'rsatadi |
|---|---|
| `/lidlar` (yoki `/statistika`) | Jami lidlar, bugungi lidlar, statuslar bo'yicha, oxirgi 5 lid |
| `/kurslar` | Kurslar ro'yxati (yoqilgan/o'chirilgan belgisi bilan) |
| `/yordam` (yoki `/admin`) | Yordam matni |

---

## 4. Admin panel imkoniyatlari

Manzil: `https://SAYTINGIZ/admin` (`ADMIN_USER` / `ADMIN_PASSWORD` bilan kiriladi).
Dizayn — **Markaz CRM tizimi**: Inter shrifti, indigo asosiy rang (#5B6CF0),
yumshoq kartalar, chap menyu paneli, rang-kodli statuslar.

| Sahifa | Imkoniyatlari |
|---|---|
| **Bugun** (asosiy) | Kunlik bloklar (chaqiruvlar, sinov darslari, boshlanayotgan guruhlar, qarzdorlar) + **Kanban CRM doskasi**: Yangi → Aloqaga chiqildi → Sinov darsi → O'qishga yozildi / Rad etildi. Drag & drop; "Rad etildi" sabab so'raydi, "O'qishga yozildi" guruh so'raydi. |
| **Lidlar** | Barcha lidlar. Avtosaqlanadigan status/izoh/mas'ul, sinov darsi sanasi, keyingi qo'ng'iroq sanasi (muddati o'tganlari qizil), **CSV eksport**. |
| **Talabalar** | Lid "O'qishga yozilganda" avtomatik yaratiladi. Qidiruv, filtrlar, shaxsiy chegirmalar, **talaba kartochkasi**: oylik narx, to'lovlar (naqd/karta/Click/Payme), oyma-oy davrlar, qarz. |
| **Qarzdorlar** | Qarzli talabalar: summa, kechikish kunlari, qarz yoki kechikish bo'yicha saralash. |
| **Guruhlar** | O'qituvchi, jadval ("Du-Chor-Ju 16:00"), boshlanish sanasi (eslatmalarni boshqaradi), **joylar sig'imi**. To'lgan guruh avtomatik "To'ldi" bo'ladi, yangi yoziluvchilar kutish ro'yxatiga tushadi. |
| **Kurslar** | Kurs kartochkalari: nom, **narx, davomiylik, o'qituvchi**, tavsif, tartib, yoqish/o'chirish. Avtosaqlanadi. |
| **O'qituvchilar** | Telefon va qisqa tarjimai hol, kurslar/guruhlar bilan bog'lanadi. |
| **Xodimlar** | Login va rol bilan xodimlar ro'yxati. |
| **AI Javoblar** | Claude javob uslubini sozlash + qo'shimcha bilimlar + **sandbox test oynasi**. |
| **Hisobot** | Oylik hisobot: lidlar, yozilishlar, konversiya %, manbalar bo'yicha, kurs daromadi, umumiy daromad va qarz, rad etish sabablari — **diagrammalar** va CSV/.xlsx eksport bilan. |
| **Shablonlar** | Tahrirlanadigan Telegram xabar shablonlari ({ism}, {kurs}, {summa}...). Lid yoki talaba qatoridan bir tugmada yuboriladi; har bir yuborish izohlar tarixiga tushadi. |
| **Jurnal (Audit)** | O'zgarmas audit jurnali: kim, nimani, qachon o'zgartirgani (eski → yangi qiymat), sana/aktor/obyekt bo'yicha filtrlash. Faqat admin. |
| **Zaxira** | Har kuni **soat 03:00 (Toshkent)** avtomatik zaxira + qo'lda zaxira va yuklab olish. Oxirgi 14 tasi saqlanadi; zaxira muvaffaqiyatsiz bo'lsa egaga Telegram xabari. |
| **Xatolar** | Server (500) xatolari jurnali — tokenlar/parollar yashirilgan. Faqat admin. |
| **Sozlamalar** | Ish vaqti (Toshkent vaqti), Owner chat ID va **Telegram eslatmalar** kaliti ("Test xabar yuborish" tugmasi bilan). |

### 4.1. PWA va xavfsizlik

- Admin panel — **PWA**: telefonda "Add to Home Screen" orqali ilova kabi
  o'rnatiladi; internetsiz "Internet yo'q" xabari ko'rsatiladi.
- 5 marta xato parol → login **15 daqiqa bloklanadi**.
- Sessiya **12 soatdan** keyin tugaydi.
- Test lid/talabalar "Test" deb belgilanadi va **bir tugmada tozalanadi**
  (haqiqiy ma'lumotlar tegilmaydi).

### 4.2. Avtomatik eslatmalar (Telegram)

- Guruh boshlanishidan **1 kun oldin**;
- Sinov darsidan **1 kun oldin**;
- Qarzdorlarga — **haftada bir marta (dushanba)**.

Barchasi faqat **ish vaqtida**, kuniga **bir marta** yuboriladi va har bir
yuborish jurnalga yoziladi.

---

## 5. Ishonchlilik

- **Webhook darhol 200 qaytaradi** — so'rov fonda ishlanadi, Telegram Claude'ni kutmaydi.
- **`update_id` orqali dublikatlar tashlanadi** — har bir Telegram so'rovi aynan bir marta ishlanadi.
- `BUSINESS_PEER_INVALID`, bloklangan bot va boshqa Telegram xatolari
  tashxisi — [README.md](README.md) dagi "Troubleshooting" jadvalida.

---

## 6. Ishga tushirish (qisqacha)

1. **@BotFather** da bot yarating → `BOT_TOKEN`.
2. Kodni **GitHub**ga yuklang, **Railway**da deploy qiling + PostgreSQL qo'shing.
3. O'zgaruvchilarni kiriting: `BOT_TOKEN`, `PUBLIC_URL`, `WEBHOOK_SECRET`,
   `ADMIN_USER`, `ADMIN_PASSWORD`, `OWNER_CHAT_ID`,
   ixtiyoriy: `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `OWNER_NOTIFY_CHAT_ID`,
   `WORK_START`, `WORK_END`.
4. Markaz telefonida **Telegram → Settings → Business → Chatbots** → botni ulang.
5. Admin panelni oching: `https://SAYT/admin`.

To'liq qadam-baqadam yo'riqnoma: [README.md](README.md).

### Muhit o'zgaruvchilari

| O'zgaruvchi | Majburiy | Tavsif |
|---|---|---|
| `BOT_TOKEN` | ✅ | @BotFather bergan token |
| `PUBLIC_URL` | ✅ | Railway sayt URL'i (oxirida `/` yo'q) |
| `WEBHOOK_SECRET` | ✅ | Webhook maxfiy kaliti |
| `ADMIN_USER` / `ADMIN_PASSWORD` | ✅ | Admin panel kiri |
| `OWNER_CHAT_ID` | ✅ | Ega Telegram ID |
| `DATABASE_URL` | ✅ (Railway) | PostgreSQL ulanishi (Railway avtomatik qo'shadi); bo'lmasa lokal SQLite |
| `OWNER_NOTIFY_CHAT_ID` | — | Lid xabarlari uchun alohida chat |
| `ANTHROPIC_API_KEY` | — | Claude AI javoblarni yoqadi |
| `CLAUDE_MODEL` | — | Standart: `claude-sonnet-4-5` |
| `CLAUDE_MAX_TOKENS` | — | Standart: `1024` |
| `WORK_START` / `WORK_END` | — | Ish vaqti (standart: 09:00 / 20:00) |

---

## 7. Lokal test (dasturchi uchun)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
set BOT_TOKEN=...
python -m uvicorn app.main:app --port 8000
```

`DATABASE_URL` bo'lmasa lokal SQLite ishlatiladi. Avtomatik smoke test:

```bash
python smoke_test.py   # oxirida ALL_SMOKE_TESTS_PASSED chiqadi
```

---

## 8. Kelajakdagi imkoniyatlar

- **Tashqi CRM integratsiyasi:** `app/crm.py` dagi `push_lead(lead)` funksiyasi
  har yangi/yangilangan lidda chaqiriladi — CRM webhook URL'ini qo'shsangiz
  lidlar avtomatik uzatiladi.
- Xodimlar **rollari bo'yicha ko'rinish** (role-based visibility) — keyingi bosqich.
