# Barcha admin sahifalarining UI tekshiruvi va tuzatish

## Maqsad
Har bir menyu sahifasida joylashuv muammolarini topish va tuzatish: chiqib ketgan (overflow) elementlar, kirib qolgan/sig'magan maydonlar, notekis joylashuv — desktop va mobil ko'rinishda.

## Qadamlar

1. **Server va tekshiruv muhiti:** lokal FastAPI serverni ishga tushirish (dummy env), admin sifatida brauzerda login.
2. **Vizual audit — 17 sahifa:** Kanban, Lidlar, Talabalar, Qarzdorlar, Kurslar, Guruhlar, O'qituvchilar, Xodimlar, Shablonlar, Jurnal, Zaxira, Xatolar, Hisobot, AI, Sozlamalar, Login. Har biri: desktop (1280px) va mobil (390px) viewport'da skrinshot; dark va light rejimlar.
3. **Topilgan muammolarni tuzatish** — kutiladigan tipik muammolar va yechimlari:
   - keng jadvallarda ustunlar siqilishi / gorizontal scroll yo'qligi → `.table-wrap` scroll yoki ustun kengliklarini moslash
   - uzun qiymatlar (ism/telefon/summa) chiqib ketishi → `overflow-wrap`, `text-overflow`
   - filtrlar qatori mobilda siqilishi → flex-wrap
   - modallar mobil ekran sig'masligi → max-height + scroll
   - formalar ustunlari notekis → grid moslash
4. **Qayta tekshiruv:** tuzatilgan sahifalarni qayta skrinshot, smoke test (`python smoke_test.py`).

## O'zgaradigan fayllar (faqat keraklisi)
`app/templates/*.html`, ehtimol `app/templates/base.html` (global CSS). Python logikasi o'zgarmaydi.

## Tekshiruv
- Har sahifa desktop + mobil skrinshotda qayta ko'riladi
- `smoke_test.py` → ALL_SMOKE_TESTS_PASSED