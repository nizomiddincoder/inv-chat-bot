# Staging muhit — Railway'da alohida sinov maydoni

> **Maqsad:** har bir o'zgarish avval staging'da sinanadi, keyin production'ga chiqadi.
> Production — boshqa baza, boshqa bot; staging'dagi xato mijozlarga hech qachon ko'rinmaydi.

---

## 1. Yangi test bot yarating (5 daqiqa)

1. Telegram'da **@BotFather** → `/newbot`.
2. Nom: `Inventor STAGING`, username: `inventor_staging_bot` (istalgan, `bot` bilan tugashi shart).
3. BotFather bergan **token**ni saqlab qo'ying — bu `BOT_TOKEN_STAGING`.
4. Test botga o'zingiz **Start** bosing (eslatmalar uchun kerak).

## 2. Railway'da ikkinchi service (10 daqiqa)

1. [railway.app](https://railway.app) → **hozirgi production loyihasini oching**.
2. Loyihaning ichida **+ New → GitHub Repo → xuddi shu repozitoriyani tanlang**
   (yoki ko'paytirish osonroq bo'lsa: production service ustiga o'ng tugma → **Duplicate**).
3. Yangi service'ga **+ New → Database → PostgreSQL** qo'shing (alohida baza!).
   `DATABASE_URL` avtomatik qo'shiladi.
4. Service → **Settings → Networking → Generate Domain** → yangi URL, masalan
   `https://inventor-staging-production.up.railway.app` — bu `PUBLIC_URL_STAGING`.

## 3. O'zgaruvchilar (service → Variables)

| O'zgaruvchi | Qiymat |
|---|---|
| `BOT_TOKEN` | **test bot** tokeni (production'dagidan farqli!) |
| `PUBLIC_URL` | staging URL (yuqorida olingan) |
| `WEBHOOK_SECRET` | yangi tasodifiy matn (production'dagidan farqli) |
| `ADMIN_USER` | masalan `admin` |
| `ADMIN_PASSWORD` | **oddiyroq parol mumkin** — bu faqat sinov muhiti |
| `OWNER_CHAT_ID` | sizning Telegram ID (xuddi shu) |
| `STAGING` | `1` — **shu o'zgaruvchi "Test ma'lumotlarni o'chirish" tugmasini yoqadi** |
| `ANTHROPIC_API_KEY` | ixtiyoriy — Claude'ni staging'da ham sinash uchun |

> `STAGING=1` bo'lsa admin panelda Lidlar va Talabalar sahifalarida
> "Test ma'lumotlarni o'chirish" tugmasi ko'rinadi. Production'da bu tugma
> yo'q va API ham 403 qaytaradi.

## 4. Botni staging hisobiga ulash (sinov uchun)

Staging botni haqiqiy mijozlar bilan ishlatilmaydigan **ikkinchi Telegram
Premium hisobiga** ulang (yoki sinov uchun o'z raqamingizda):

1. O'sha telefon: **Telegram → Settings → Business → Chatbots → Add bot**.
2. `inventor_staging_bot` ni tanlang va javob berishga ruxsat bering.

Agar ikkinchi Premium hisob yo'q bo'lsa: staging botni oddiy shaxsiy chatda
sinang (`/start`, kurs tugmalari, ism → telefon oqimi) — business rejimsiz ham
butun oqim ishlaydi.

## 5. Ish tartibi (deploy disiplini)

```
Kod o'zgaradi → lokal: python smoke_test.py
            → git push → Railway ikkala service ham qayta deploy qiladi
            → staging'da tekshirish (admin panel + bot)
            → hammasi toza bo'lsa production'da tekshirish
```

- Railway **ikkala service** bir xil repodan deploy qiladi — shuning uchun
  production'ga chiqishdan oldin staging URL'da tekshirish shart.
- Har bir katta o'zgarishdan oldin production admin panelida **Zaxira olish**
  tugmasini bosing va faylni yuklab oling.

## 6. Production'dagi test yozuvlarni tozalash

Tozalash faqat **staging'da** ishlaydi, shuning uchun production'dagi test
yozuvlar tartibi:

1. Production: **Zaxira olish** → nusxani yuklab oling.
2. Staging: **Zaxira > Tiklash** orqali production nusxasini staging bazasiga tiklang
   (endi staging = production nusxasi).
3. Staging: "Test" belgili lid/talabalarga belgi qo'ying (belgilanmaganlarni
   belgilang) → **Test ma'lumotlarni o'chirish**.
4. Natijani tekshiring. Audit jurnal o'chmaydi — o'chirish jurnalga yoziladi.

Production'da to'g'ridan-to'g'ri tozalash kerak bo'lsa — alohida ko'rsatma
bilan qo'lda bajariladi (xavfsizlik uchun tugma production'da mavjud emas).

## 7. Zaxira tiklash mashqi (oylik tavsiya)

Oyda bir marta staging'da production zaxirasini **tiklash** tugmasi bilan
tiklang — zaxiralar ishlayotganini va restore tartibini bilib turish uchun.
Tiklashdan oldin avtomatik xavfsizlik nusxa olinadi, xato bo'lsa qayta tiklash mumkin.
