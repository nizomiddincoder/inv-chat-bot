# Inventor Innovation Center — Telegram Business Bot

Bot for the "Inventor Innovation Center" training center (Uzbekistan).
It chats with visitors through **Telegram Business → Chatbots** (business mode),
collects course leads (course → name → phone), stores them in PostgreSQL,
and shows them in a simple admin panel.

- Bot messages and admin panel: **Uzbek (Latin)**
- Admin panel: **https://YOUR-RAILWAY-URL/admin** (login + password)
- No paid services needed beyond Railway + its PostgreSQL.

---

## Project structure

```
inv-chat-bot/
├── app/
│   ├── main.py              # FastAPI app: webhook, health check, startup
│   ├── config.py            # reads environment variables
│   ├── database.py          # DB engine, table creation, default data
│   ├── models.py            # DB tables: leads, courses, messages, settings...
│   ├── services.py          # business logic: flow state, validation, leads
│   ├── handlers.py          # answers every Telegram update (the conversation)
│   ├── telegram_client.py   # calls the Telegram Bot API with httpx
│   ├── crm.py               # CRM adapter (TODO: connect external CRM here)
│   ├── followup.py          # 24-hour follow-up reminders to the owner
│   ├── admin/
│   │   └── routes.py        # admin panel pages (/admin)
│   └── templates/           # admin panel HTML (Uzbek)
├── smoke_test.py            # local test: simulates a full chat + admin check
├── requirements.txt
├── Procfile                 # start command for Railway
└── README.md
```

Tables are created **automatically** on the first start — no migration commands needed.

---

## Part 1 — Create the Telegram bot (5 minutes)

1. Open Telegram, find **@BotFather**, send `/newbot`.
2. Choose a display name, e.g. `Inventor Innovation Center`.
3. Choose a username ending in `bot`, e.g. `inventor_center_bot`.
4. BotFather replies with a **token** like `1234567890:AAE...`. Copy it — this is your `BOT_TOKEN`.
5. Get your owner chat id: open **@userinfobot**, send it any message. It replies with `Id: 123456789`. This number is your `OWNER_CHAT_ID`.
6. **Important:** open your own bot and press **Start** once. Telegram only lets a bot
   message people who have started a chat with it. Do this so the bot can send you lead notifications.

## Part 2 — Put the code on GitHub

1. Create a free account on [github.com](https://github.com) and a new repository (e.g. `inventor-bot`).
2. Upload this whole project folder to the repository (GitHub web → "uploading an existing file").

## Part 3 — Deploy on Railway (10 minutes)

1. Go to [railway.app](https://railway.app) and sign in with GitHub.
2. **New Project → Deploy from GitHub repo** → pick `inventor-bot`.
3. In the project, click **+ New → Database → Add PostgreSQL**. Railway creates the
   database and adds the `DATABASE_URL` variable automatically.
4. Click your app service → **Settings** → **Networking → Generate Domain**.
   Railway gives you a public URL like `https://inventor-bot-production.up.railway.app`.
   Copy it — this is your `PUBLIC_URL`.
5. Open the app service → **Variables** and add:

   | Variable         | Value                                                              |
   |------------------|--------------------------------------------------------------------|
   | `BOT_TOKEN`      | the token from @BotFather                                          |
   | `PUBLIC_URL`     | `https://inventor-bot-production.up.railway.app` (your URL, no `/` at the end) |
   | `WEBHOOK_SECRET` | any long random text, e.g. `inv-9f3k2jd8s1kq7hz` (keep it private) |
   | `ADMIN_USER`     | your admin login, e.g. `admin`                                     |
   | `ADMIN_PASSWORD` | a strong password for the admin panel                              |
   | `OWNER_CHAT_ID`  | your Telegram id from @userinfobot, e.g. `123456789`               |
   | `WORK_START`     | `09:00`                                                            |
   | `WORK_END`       | `20:00`                                                            |

   (`DATABASE_URL` is already there from step 3.)

6. Railway redeploys automatically after each variable change. Open the
   **Deployments → View Logs** tab. You should see:

   ```
   db: Database ready: postgresql://...
   telegram: Webhook set successfully: https://.../webhook
   bot.followup: Follow-up thread started
   ```

   If you see `Webhook set successfully`, Telegram already knows where to send messages. ✅

7. Optional check in a browser: open
   `https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo` — it should show your
   Railway URL and `last_error_message` should be empty.

## Part 4 — Connect Telegram Business (the key step!)

The bot must be connected to the center's **Premium** Telegram account:

1. On the center's phone, open **Telegram → Settings → Business → Chatbots**.
2. Tap **Add bot**, choose your bot, and enable the chats where it should reply.
3. Now every private message to the center's account is answered by the bot.
   You will see the bot's replies right in the same chat.

> Note: the bot replies only in **private chats** and never to your own messages —
> you can keep writing to customers normally from the same account.

## Part 5 — Admin panel

Open `https://YOUR-RAILWAY-URL/admin` and log in with `ADMIN_USER` / `ADMIN_PASSWORD`.

| Page | What it does |
|------|--------------|
| **Lidlar** | All leads. Filter by course/status, search by name or phone, change status, add notes, download CSV (opens in Excel). |
| **Kurslar** | Add / edit / delete / enable / disable courses. Changes the bot's buttons **immediately**. |
| **Xabarlar** | Edit every bot text (welcome, after course, after name, wrong phone, finish, out-of-hours). Use `{first_name}` and `{course}` placeholders. |
| **Sozlamalar** | Working hours (e.g. 09:00–20:00, Tashkent time) and Owner chat ID. |

Outside working hours the bot adds: *"Hozir ish vaqtimiz tugagan, ertaga javob beramiz."*

---

## How the conversation works

```
Visitor:  (any message)
Bot:      Assalomu alaykum ... kurslardan birini tanlang: [buttons]
Visitor:  (taps a course button or types 1 / course name)
Bot:      Javobingiz uchun rahmat! Ismingizni yozib qoldiring.
Visitor:  Aziza Karimova
Bot:      Telefon raqamingizni qoldiring, admin tez orada aloqaga chiqadi.
Visitor:  +998901234567   (also OK: 998901234567, 901234567)
Bot:      Rahmat! Ma'lumotlaringiz qabul qilindi, admin tez orada bog'lanadi.
Owner:    gets a private message: name, phone, course, username, time
```

- `/start` or **"bekor qilish"** restarts the flow at any moment.
- If the visitor **edits** a message while the bot waits for the phone number,
  the edited name replaces the stored one and the bot confirms it. Other edits
  are ignored so they never create duplicate leads.
- The same Telegram user or same phone never creates a duplicate lead — the
  existing one is updated.
- **24-hour follow-up:** if an open lead gets no reply for 24 hours, the bot
  sends **one** reminder to the *owner* (never to the visitor).

## Admin commands inside Telegram (owner only)

The owner can send these commands to the bot (in the bot's own chat or right in
the business chat). They work **only** for the account whose ID is in
`OWNER_CHAT_ID` (env variable or the admin panel → Sozlamalar):

| Command | What it shows |
|---------|---------------|
| `/lidlar` (or `/statistika`) | Total leads, today's leads, counts by status, last 5 leads |
| `/kurslar` | Course list with enabled/disabled marks |
| `/yordam` (or `/admin`) | This help text |

Everyone else who types these commands gets *"Bu buyruq faqat admin uchun 🙅"*
(in the bot chat) or simply the normal welcome flow (in the business chat).
The restriction affects **only** these commands — the visitor flow never
depends on `OWNER_CHAT_ID`.

> Note: the bot also ignores the business account owner's normal (non-command)
> messages, so you can keep chatting with customers yourself.

## Test checklist (do this after deploying)

1. Ask a friend (second Telegram account) to message the center's account.
2. ✅ Bot replies with the welcome text + course buttons.
3. ✅ Tapping a course button asks for the name.
4. ✅ Typing a name (2–50 letters) asks for the phone.
5. ✅ Typing a wrong phone (e.g. `123`) → the bot asks again politely.
6. ✅ Typing a valid phone → finish message appears.
7. ✅ You (owner) receive a lead notification in your private bot chat.
8. ✅ Lead appears in `/admin/leads` with the correct course, name and phone.
9. ✅ Ask the same friend to go through the flow again → **no duplicate lead**, the existing row is updated.
10. ✅ In **Kurslar** disable a course → the friend sends "bekor qilish" → the disabled course no longer shows as a button.
11. ✅ Change a text in **Xabarlar** → the friend sends "bekor qilish" → the new text arrives.
12. ✅ Export CSV from the Lidlar page and open it in Excel — Uzbek letters look correct.

## Local testing (optional, for a developer)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
set BOT_TOKEN=...             # or create a .env-style script
python -m uvicorn app.main:app --port 8000
```

Without `DATABASE_URL` it uses a local SQLite file, so the admin panel and flow
can be tested even before Railway.

There is also an automated smoke test (`pip install fastapi httpx` is enough):

```bash
python smoke_test.py
```

It simulates Telegram updates (welcome, button press, name, phone, duplicate
lead) against a local SQLite database and prints `ALL_SMOKE_TESTS_PASSED` on success.

## Troubleshooting (Railway logs)

| Problem in logs | Meaning / fix |
|---|---|
| `Missing environment variables: ...` | Set the listed variables in Railway → Variables. |
| `Webhook set successfully` is missing | `PUBLIC_URL` is wrong (no `https://`, or trailing `/`) or `WEBHOOK_SECRET` is empty. |
| `403 Forbidden` spam in logs | Something is calling `/webhook` without the secret — harmless, Telegram requests will still work. |
| `Telegram API sendMessage failed: ...` | Read the description. Common: `bot was blocked by the user`, or the owner never pressed Start in the bot chat. |
| `BUSINESS_PEER_INVALID` | The reply was sent to a chat that does not belong to the connected business account. Check: **Settings → Business → Chatbots** — disconnect and reconnect the bot (required after changing bot settings in @BotFather); make sure **Business Mode** is enabled in @BotFather; make sure the chat is not excluded in the Chatbots chat list. The bot retries once without the connection id and logs the details. |
| `Forbidden: bot was blocked by the user` | This person **blocked the bot** in Telegram. A blocked bot also cannot reply through the business connection (you will see `BUSINESS_PEER_INVALID` for the same chat first). Fix: the user opens the bot's chat and presses **Unblock / Start**, then sends `/start`. Then business replies work again. |
| `Unauthorized` from Telegram | Wrong `BOT_TOKEN`. |
| Admin panel asks for login forever | Wrong `ADMIN_USER` / `ADMIN_PASSWORD` values. |

## Connecting an external CRM (later)

Open `app/crm.py`. The `push_lead(lead)` function is called for every new or
updated lead. A ready-made commented example inside shows how to POST the lead
to a CRM webhook — just paste your CRM's URL and API key and uncomment it.
